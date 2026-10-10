"""Browser agent by voice: tasks on sites in the user's own browser, the page they are reading, scheduled checks.

"найди на озоне наушники до 3000 с хорошими отзывами", "включи на ютубе обзор iPhone 17",
"перескажи эту статью", "о чём это видео", "каждый день в 9 проверяй цену на AirPods на озоне",
"покажи браузер", "останови задачу в браузере", "подключи браузер".
The task runs in the background: Jarvis keeps listening and answers other requests meanwhile.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re

from assistant.core import Card, Deck
from assistant.log import private
from assistant.skills.base import Intent, Reply, Skill, Tool, run_blocking
from assistant.web import policy
from assistant.web.agent import Job, Outcome, WebAgent, parse_json
from assistant.web.bridge import EXTENSION_DIR, Bridge, ensure_key
from assistant.web.jobs import Scheduler, WebJob
from assistant.web.routes import BUILTIN, Routes
from assistant.when import parse_when, say_when

log = logging.getLogger("web")

_ALIASES = sorted({a for s in BUILTIN for a in s.aliases if len(a) > 2 or a in ("вб",)}, key=len, reverse=True)
_SHOPS = sorted({a for s in BUILTIN if s.kind == "shop" for a in s.aliases}, key=len, reverse=True)
_VIDEO = sorted({a for s in BUILTIN if s.kind == "video" for a in s.aliases}, key=len, reverse=True)
_END = r"(?:е|у|а|ом|ах)?(?=\s|$)"   # "на озоне", "на вб", but not "вбей" or "маркетинг"
_SHOP = rf"(?:{'|'.join(map(re.escape, _SHOPS))}){_END}"
_VIDEO_SITE = rf"(?:{'|'.join(map(re.escape, _VIDEO))}){_END}"
_ANY_SITE = rf"(?:{'|'.join(map(re.escape, _ALIASES))}){_END}"

# A shop is named: always the agent ("найди на озоне …", "сколько стоит … на вб", "положи в корзину на маркете …").
_SHOP_TASK = re.compile(rf"(?:^|\s)(?:на|в|во|с|из)\s+{_SHOP}(?:\s|$)|(?:^|\s){_SHOP}\s+(?:найди|поищи|подбери)")
# A video site with "включи": find and play ("найди на ютубе …" alone stays the instant search page).
_PLAY = re.compile(rf"^(?:включи|поставь|запусти|найди и включи|открой и включи)\s.*(?:на|в)\s+{_VIDEO_SITE}"
                   rf"|^(?:включи|поставь|запусти)\s+(?:на\s+)?{_VIDEO_SITE}\s+\S")
# "включи звук в браузере", "открой браузер" belong to other modules: only task verbs here.
_BROWSER_VERB = (r"(?:сделай|выполни|зайди|проверь|посмотри|узнай|прочитай|почитай|собери|сравни|подбери|закажи|"
                 r"оформи|найди и|выбери|отсортируй|отфильтруй)")
_IN_BROWSER = re.compile(rf"^{_BROWSER_VERB}(?:\s.*)?\s(?:в браузере|на сайте|через браузер)(?:\s|$)|"
                         rf"^(?:в браузере|через браузер)\s+\S|^зайди на (?:сайт\s+)?\S+\s+и\s+")
_CART = re.compile(r"\b(?:добавь|положи|закинь)\b.*\b(?:в корзину|в избранное)\b")
_SEARCH_ONLY = re.compile(r"^(?:найди|найти|поищи|ищи|загугли|погугли)\s+(?:в|на)\s+(?:браузере|интернете|сети|яндексе|"
                          r"гугле)\s")

_PAGE = r"(?:статья|статью|статье|страница|страницу|странице|видео|ролик|ролике|вкладка|вкладку|вкладке|сайт|сайте|пост|посте)"
_READ = re.compile(
    rf"^(?:кратко |коротко |быстро )?(?:перескажи|суммаризируй|сделай выжимку из|сделай конспект|законспектируй|"
    rf"резюмируй|прочитай и перескажи|расскажи о чем|расскажи что в)\s+(?:мне\s+)?(?:эт\w+\s+|текущ\w+\s+|открыт\w+\s+)?"
    rf"{_PAGE}(?:\s.*)?$|"
    rf"^о ч[её]м (?:эт\w+|текущ\w+|открыт\w+) {_PAGE}(?:\s.*)?$|"
    rf"^(?:что|какие|какая|какой|сколько|где|когда|кто|есть ли)(?:\s.*)?\s(?:на|в|во) (?:эт\w+|текущ\w+|открыт\w+) {_PAGE}"
    rf"(?:\s.*)?$|^(?:на|в) (?:эт\w+|текущ\w+) {_PAGE}\s+\S")

_SETUP = re.compile(r"^(?:подключи|установи|настрой|поставь)\s+(?:расширение(?: для)?(?: браузера)?|браузер|"
                    r"связь с браузером|агента браузера|браузерного агента)$|^как подключить (?:браузер|расширение)")
_SHOW = re.compile(r"^покажи\s+(?:окно\s+)?(?:браузер\w*|агента|что наш[её]л|что нашел в браузере|"
                   r"результат в браузере|окно агента)$")
_HIDE = re.compile(r"^(?:спрячь|сверни|убери)\s+(?:окно\s+)?(?:браузер\w*|агента|окно агента)$")
_CANCEL = re.compile(r"^(?:останови|отмени|прекрати|прерви|брось|хватит)\s+(?:задачу\s+(?:в\s+)?браузер\w*|поиск\w*|"
                     r"искать|агента|браузерного агента|работу в браузере|что делаешь в браузере)$|^хватит искать$")
_STATUS = re.compile(r"^(?:что|как)\s+(?:там\s+)?(?:с\s+)?(?:поиском|задачей в браузере|браузером|агентом)$|"
                     r"^ты (?:еще|все еще) ищешь$|^(?:долго еще|скоро)( там)?$")
_EVERY = re.compile(r"\b(?:кажд\w+|ежедневно|по будням|по выходным|раз в день)\b")
_WATCH_VERB = re.compile(r"\b(?:проверяй|проверь|смотри|посмотри|следи|отслеживай|мониторь|узнавай|узнай|ищи|найди|"
                         r"проверять|следить|отслеживать)\b")
_WATCH = re.compile(r"^(?:следи|отслеживай|мониторь)\s+за\s+")
_JOBS_LIST = re.compile(r"^(?:какие|покажи|список)\s+(?:у меня\s+)?(?:задачи|проверки|слежки)\s*(?:в браузере|по расписанию|"
                        r"браузера)?$|^что ты (?:проверяешь|отслеживаешь)")
_JOBS_CANCEL = re.compile(r"^(?:отмени|удали|убери|выключи|останови)\s+(?:все\s+)?(?:проверк\w+|слежк\w+|отслеживани\w+|"
                          r"задач\w+ по расписанию)(?:\s+(?P<what>.+))?$|^(?:перестань|хватит)\s+следить\s+за\s+(?P<what2>.+)$")

COMPARE_PROMPT = """Пользователь поручил регулярно: «{task}».
Прошлый результат: {prev}
Новый результат: {new}
Нужно ли сообщить пользователю сейчас? Если в поручении есть условие («если подешевеет», «если появится», «когда
выйдет», «ниже N») — только когда оно выполнено (сравни с прошлым результатом). Условия нет — сообщай всегда.
Ответь только JSON: {{"notify": true или false, "speech": "1–2 коротких предложения для голоса: что сейчас и что
изменилось (было → стало)"}}"""

SETUP_CARD = """Один раз, минута:
1. В Яндекс Браузере откройте адрес browser://extensions (в Chrome — chrome://extensions)
2. Включите «Режим разработчика» справа вверху
3. Нажмите «Загрузить распакованное расширение» и выберите папку (путь уже в буфере обмена):
`{path}`
4. Готово: значок «Джарвис» без надписи «off» — связь есть
- Джарвис должен быть запущен: ключ связи он кладёт в эту папку сам
- Агент работает в отдельном свёрнутом окне и ваши вкладки только читает — по просьбе
- Пароли, коды, карты, банки и Госуслуги он не трогает; заказ, отправку и удаление — только после вашего «да»"""


def _strip_schedule(text: str, spans: tuple[str, ...]) -> str:
    for piece in spans:
        text = text.replace(piece, " ", 1)
    text = _EVERY.sub(" ", text)
    text = re.sub(r"\b(?:в|во)\s+\d{1,2}(?::\d\d)?\b|\b(?:утром|вечером|днем|каждое утро|каждый вечер)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


class WebSkill(Skill):
    name = "web"
    title = "Браузер: задачи на сайтах"
    examples = [
        'найди на озоне <товар>',
        'сравни цены на <товар> на маркете',
        'положи в корзину на вб <товар>',
        'включи на ютубе <видео>',
        'сделай в браузере <задача>',
        'перескажи эту статью',
        'о чем это видео',
        'каждый день в <ЧЧ:ММ> проверяй цену <товар> на озоне',
        'какие проверки в браузере',
        'покажи браузер',
        'останови задачу в браузере',
        'подключи браузер',
    ]

    def __init__(self) -> None:
        self.bridge: Bridge | None = None
        self.agent: WebAgent | None = None
        self.scheduler: Scheduler | None = None

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        cfg = self.app.cfg.web
        if not cfg.enabled:
            return
        key = await run_blocking(ensure_key)
        self.bridge = Bridge(cfg.port, key)
        self.bridge.on_event = self._on_event
        self.agent = WebAgent(self.app, self.bridge, Routes())
        try:
            await self.bridge.start()
        except OSError as exc:
            log.warning("Связь с браузером не запущена (порт %d занят?): %s", cfg.port, exc)
        self.scheduler = Scheduler(self._scheduled)
        self.scheduler.start()
        # The extension's icon takes Jarvis's colors: listening, thinking, speaking, microphone off.
        self.app.state_listeners.append(lambda state: self.bridge.notify("state", state=state.value))

    async def stop(self) -> None:
        if self.scheduler is not None:
            self.scheduler.stop()
        if self.bridge is not None:
            await self.bridge.stop()

    def _on_event(self, event: str, data: dict) -> None:
        if event == "connected":
            self.bridge.notify("state", state=self.app.state.value)
        elif event == "wake":   # the extension's button in the browser toolbar: the same as saying "Джарвис"
            log.info("Кнопка Джарвиса в браузере")
            self.app.wake_up()
        elif event == "window_closed" and self.agent is not None and self.agent.cancel():
            log.info("Окно агента закрыто пользователем — задача остановлена")

    # ---------------------------------------------------------------- matching
    def match(self, text: str) -> Intent | None:
        if _SETUP.search(text):
            return Intent(self.name, "setup", text=text)
        if _SHOW.match(text):
            return Intent(self.name, "show", text=text)
        if _HIDE.match(text):
            return Intent(self.name, "hide", text=text)
        if _CANCEL.match(text):
            return Intent(self.name, "cancel", text=text)
        if _STATUS.match(text) and self.agent is not None and self.agent.busy:
            return Intent(self.name, "status", text=text)
        if _JOBS_LIST.search(text):
            return Intent(self.name, "jobs", text=text)
        m = _JOBS_CANCEL.match(text)
        if m:
            return Intent(self.name, "unschedule", {"what": m.group("what") or m.group("what2") or ""}, text)
        if _READ.match(text):
            return Intent(self.name, "read", text=text)
        site = bool(_SHOP_TASK.search(text) or re.search(rf"\s(?:на|в)\s+{_ANY_SITE}(?:\s|$)", text))
        if (_EVERY.search(text) and _WATCH_VERB.search(text) and (site or "цен" in text)) or \
                (_WATCH.match(text) and (site or "цен" in text)):
            return Intent(self.name, "schedule", text=text)
        if _SEARCH_ONLY.match(text):
            return None   # "найди в интернете …": the instant search page (search skill) or an answer
        if _SHOP_TASK.search(text) or _PLAY.match(text) or _IN_BROWSER.search(text) or (_CART.search(text) and site):
            return Intent(self.name, "task", text=text)
        return None

    def tools(self) -> list[Tool]:
        return [Tool("browser_task", "Выполнить задачу на сайтах в браузере пользователя: найти и сравнить товары "
                     "на маркетплейсах, найти и включить видео, прочитать страницы. Работает в фоне.",
                     {"type": "object", "properties": {"task": {"type": "string", "description": "задача целиком"}},
                      "required": ["task"]}, "tool")]

    # ---------------------------------------------------------------- handling
    async def handle(self, intent: Intent) -> Reply:
        if self.agent is None or self.bridge is None:
            return Reply("Браузерный агент выключен в настройках.")
        action = intent.action
        if action == "setup":
            return await self._setup(already=self.bridge.connected)
        if action == "cancel":
            return Reply("Остановил задачу в браузере." if self.agent.cancel() else "В браузере я сейчас ничего не делаю.",
                         listen_after=False)
        if action == "status":
            return Reply(f"Работаю над {self.agent.status()}.", listen_after=False)
        if action == "jobs":
            return self._jobs()
        if action == "unschedule":
            return self._unschedule(str(intent.slots.get("what") or ""))
        if not await self.bridge.wait_connected(2.0):
            return await self._setup(already=False)
        if action == "show":
            return Reply(reaction="ok", speech="Показываю.") if await self.agent.show() else \
                Reply("Окна агента сейчас нет.")
        if action == "hide":
            await self.agent.hide()
            return Reply(reaction="ok", speech="Свернул.", listen_after=False)
        if action == "read":
            return await self._read(intent.raw or intent.text)
        task = str(intent.slots.get("task") or intent.raw or intent.text)
        if action == "schedule":
            return self._schedule(intent.text, task)
        return self._start(task)

    def _start(self, task: str) -> Reply:
        refusal = policy.check_task(task)
        if refusal:
            return Reply(refusal)
        if not self.agent.start(task, self._done):
            return Reply(f"Я ещё занят в браузере: {self.agent.status()}. Скажите «останови задачу в браузере», "
                         "если она уже не нужна.")
        sites = self.agent.routes.mentioned(task)
        where = f" на {sites[0].name}" if sites and sites[0].kind != "search" else ""
        verb = "Ищу" if re.search(r"\b(найди|поищи|подбери|сравни|узнай|посмотри|выбери)", task.lower()) else "Займусь"
        return Reply(f"{verb}{where}, сэр. Скажу, когда будет готово.", tool_result="задача запущена в фоне",
                     listen_after=False)

    async def _done(self, outcome: Outcome) -> None:
        self._show_card(outcome)
        await self.app.announce(outcome.speech)
        self.app.ui.chat_add("assistant", outcome.speech + (f"\n\n{outcome.card}" if outcome.card else ""))

    def _show_card(self, outcome: Outcome) -> None:
        if outcome.card:
            card = outcome.card + (f"\n\n[Открыть страницу]({outcome.url})" if outcome.url and
                                   outcome.url not in outcome.card else "")
            self.app.ui.show_deck(Deck(title=outcome.title, cards=[Card("", card, "")], done=True, pinned=True))

    async def _read(self, question: str) -> Reply:
        try:
            outcome = await self.agent.read_current(question)
        except Exception as exc:  # noqa: BLE001 - a bridge or model failure is told, not raised
            log.warning("Страница не прочитана: %s", exc)
            return Reply("Не получилось прочитать страницу.")
        self._show_card(outcome)
        return Reply(outcome.speech, tool_result=outcome.card[:800] or outcome.speech)

    async def _setup(self, already: bool) -> Reply:
        from assistant import winutil

        if already:
            return Reply("Браузер уже подключён, сэр.")
        path = str(EXTENSION_DIR)
        await run_blocking(ensure_key)
        try:
            await run_blocking(winutil.set_clipboard_text, path)
            await run_blocking(os.startfile, path)
        except Exception as exc:  # noqa: BLE001 - only convenience
            log.debug("Папка расширения не открыта: %s", exc)
        self.app.ui.show_deck(Deck(title="Подключить браузер", cards=[Card("", SETUP_CARD.format(path=path), "")],
                                   done=True, pinned=True))
        return Reply("Расширение для браузера ещё не подключено. Показал, как поставить его за минуту.",
                     listen_after=False)

    # ---------------------------------------------------------------- scheduled checks
    def _schedule(self, norm: str, raw: str) -> Reply:
        when = parse_when(norm)
        now = dt.datetime.now()
        if when is not None:
            at, repeat, spans = when.at, when.repeat, when.spans
            if not repeat and (_EVERY.search(norm) or _WATCH.match(norm)):
                repeat = "daily"
        else:
            hour = 9 if "утр" in norm else 19 if "вечер" in norm else 10
            at = dt.datetime.combine(now.date(), dt.time(hour, 0))
            at = at if at > now else at + dt.timedelta(days=1)
            repeat, spans = ("weekdays" if "будн" in norm else "weekends" if "выходн" in norm else "daily"), ()
        task = _strip_schedule(norm, spans)
        refusal = policy.check_task(task)
        if refusal:
            return Reply(refusal)
        job = self.scheduler.add(task, at, repeat)
        log.info("Проверка %s: %s", job.id, private(task))
        return Reply(f"Хорошо, буду проверять {say_when(at, repeat=repeat)}. Скажу, если будет что сообщить.",
                     listen_after=False)

    def _jobs(self) -> Reply:
        jobs = list(self.scheduler.jobs.values()) if self.scheduler else []
        if not jobs:
            return Reply("Проверок по расписанию нет.")
        lines = [f"- {j.task} — {say_when(dt.datetime.fromtimestamp(j.due), repeat=j.repeat)}" for j in jobs]
        self.app.ui.show_deck(Deck(title="Проверки в браузере", cards=[Card("", "\n".join(lines), "")], done=True,
                                   pinned=True))
        n = len(jobs)
        return Reply(f"Проверок: {n}. Список на экране." if n > 1 else f"Одна: {jobs[0].task}.")

    def _unschedule(self, what: str) -> Reply:
        jobs = list(self.scheduler.jobs.values()) if self.scheduler else []
        if not jobs:
            return Reply("Проверок по расписанию нет.", listen_after=False)
        words = [w for w in re.findall(r"\w{3,}", what.lower()) if w not in ("цены", "цену", "ценой", "все")]
        if words:
            hits = [j for j in jobs if any(w[:5] in j.task.lower() for w in words)]
        else:
            hits = jobs if (len(jobs) == 1 or "все" in what) else []
        if not hits:
            return Reply("Не понял, какую проверку отменить. Скажите, например: «отмени проверку цены на наушники».")
        for j in hits:
            self.scheduler.remove(j.id)
        return Reply("Отменил." if len(hits) == 1 else f"Отменил проверок: {len(hits)}.", reaction="ok",
                     listen_after=False)

    async def _scheduled(self, job: WebJob) -> None:
        if self.agent is None or self.bridge is None:
            return
        for _ in range(40):          # the user's own task first
            if not self.agent.busy:
                break
            await asyncio.sleep(30)
        if not await self.bridge.wait_connected(60):
            log.info("Проверка «%s» пропущена: браузер не подключён", private(job.task))
            return
        outcome = await self.agent.run(Job(job.task, quiet=True))
        if not outcome.ok:
            log.info("Проверка «%s» не удалась: %s", private(job.task), outcome.speech)
            return
        new = outcome.summary or outcome.speech
        prompt = COMPARE_PROMPT.format(task=job.task, prev=job.last_summary or "(это первая проверка)",
                                       new=f"{new}\n{outcome.card[:1500]}")
        verdict = parse_json(await self.agent._think(prompt, 400)) or {"notify": True, "speech": outcome.speech}
        job.last_summary = new
        if self.scheduler is not None:
            self.scheduler.save()
        if verdict.get("notify"):
            outcome.speech = str(verdict.get("speech") or outcome.speech)
            await self._done(outcome)
        else:
            log.info("Проверка «%s»: без изменений", private(job.task))


def create() -> Skill:
    return WebSkill()
