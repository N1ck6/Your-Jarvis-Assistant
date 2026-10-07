"""Request routing: skill fast path -> cloud (facts / internet) -> local model with tools."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections import deque
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable

from assistant.llm.hub import FAIL_TEXT
from assistant.llm.providers import LocalTurn, Msg
from assistant.log import private
from assistant.nlu import NO, YES, normalize_command, split_compound
from assistant.router import Router, Turn
from assistant.skills.base import Intent, Reply, Skill, Tool

if TYPE_CHECKING:
    from assistant.assistant import Assistant

log = logging.getLogger("brain")

_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

BASE_PROMPT = """Ты — {name}, личный ассистент пользователя на его компьютере, как J.A.R.V.I.S. у Тони Старка:
собранный, точный, спокойный, с лёгкой сухой иронией, всегда на стороне хозяина. Ответы озвучиваются голосом.
Главное — скорость и польза: сразу давай ровно то, что нужно, 1–3 коротких предложения, без вступлений и воды.
Иногда обращайся к пользователю «{address}», но не в каждой фразе. Говори по-русски, в мужском роде.
Не называй себя по имени: своё имя из динамиков ты бы услышал как обращение к себе.
В устной части никакого markdown, списков, эмодзи, ссылок, источников и формул в LaTeX (никаких знаков $ и обратных
косых черт): формулу в устной части говори словами («икс в квадрате»). Если не знаешь — так и скажи.
{screen}
Сейчас {now}, {weekday}. Город пользователя по умолчанию: {city}.{facts}"""

# Presentation principle: the screen shows what is better read (formula, code, numbers, names, steps),
# the voice explains it in other words. The part after ### is never spoken (assistant/assistant.py).
SCREEN_ADDON = """Если по ответу есть что прочитать, сохранить или использовать — формула, код или команда, числа,
цены и даты, названия, адреса, шаги, сравнение вариантов, характеристики, — после устного ответа поставь отдельную
строку ### и под ней карточку для окна на экране. Карточка — полноценная шпаргалка, а не повтор пары слов:
5–12 строк с конкретикой, которая скорее всего понадобится человеку дальше:
- точные данные: числа с единицами, даты, цены, версии, названия и имена;
- как сделать: шаги по порядку, команды и код, настройки, куда нажать;
- варианты и сравнение: чем отличаются, что выбрать и почему; плюсы, минусы, подводные камни;
- пример или готовый результат, который можно сразу применить или скопировать.
Не пиши общих слов и воды («это важная тема», «есть много вариантов»): каждая строка — новый полезный факт.
Формат строк: «- Термин — пояснение», «1. шаг», «> формула», заголовок раздела «## …» при нескольких частях.
Код — одним блоком между строками ``` с отступами, без пояснений внутри. Устная часть не зачитывает экран, а
коротко поясняет главное простыми словами, как докладчик рядом со слайдом. Для разговора, шуток и ответов в одно
слово или число ### не нужен.
Формулы и на экране пиши обычным текстом с символами Unicode (y = x², √x, π, ≤, →), без LaTeX и знаков $.
Никогда не рисуй молекулы, графики и схемы символами (|, /, \\, -): картинки показываются отдельно."""

# When a picture (graph, structure, photo) opens next to the answer.
PICTURE_ADDON = {
    "plot": "Рядом с ответом уже открыт график этой функции.",
    "molecule": "Рядом с ответом уже открыта структурная формула этой молекулы.",
    "photo": "Рядом с ответом уже открыта фотография.",
}
PICTURE_TAIL = (" Не описывай, как она нарисована, и не пиши её текстом; на экран — только факты, которых на картинке "
                "нет (формула, числа, свойства, применение).")

INFO_ADDON = """Если вопрос про факты, новости, цены, курсы, события или что-то свежее — найди в интернете.
Дай сам ответ сразу, без вступлений вроде «Согласно источникам»."""

LOCAL_ADDON = """У тебя есть инструменты для действий на компьютере. Если пользователь просит что-то сделать —
вызови подходящий инструмент, а не описывай действие словами. Никогда не выдумывай результат действия.
Получив результат инструмента, не повторяй его дословно: сразу ответь на вопрос пользователя 1–2 фразами.
Если нужен свежий факт из интернета — вызови ask_internet."""

# Questions that need facts or fresh data go to the cloud.
_INFO = re.compile(
    r"^(что такое|кто так(ой|ая|ие)|кто (был|была|написал|изобрел|изобрёл|создал|сейчас)|что значит|что означает|"
    r"когда|где|сколько|какой|какая|какое|какие|каков|почему|зачем|чем|откуда|правда ли|есть ли|"
    r"найди|узнай|поищи|загугли|посмотри в интернете)"
    r"|новост|курс (доллара|евро|рубля|биткоина)|сколько стоит|цена|счёт матча|счет матча|кто выиграл|последн(ие|яя|ий)"
)
# Small talk stays local even if it looks like a question.
_SMALLTALK = re.compile(
    r"^(как (у тебя )?дела|как ты|как настроение|как жизнь|привет|здравствуй|доброе утро|добрый (день|вечер)|спасибо|"
    r"пока|расскажи (шутку|анекдот|что-нибудь)|ты кто|как тебя зовут|что делаешь|чем занят)"
)
# A second action in the same phrase without its verb: "...и другой через две", "...и в Питере", "...и ещё одну".
_ELLIPSIS = re.compile(r"\s(?:и|а|а потом|потом|затем|а также|плюс)\s+(?:другой|другую|другое|второй|вторую|третий|"
                       r"еще одн\w*|еще|тоже|также|то же самое|в|во|на|через|для|про)\s")
CONFIRM_TTL_SEC = 20
# Commands about other commands: they never become "the last action".
_META = {"again", "repeat", "stop", "wrong", "undo", "thanks"}


def _chain_undo(undos: list) -> Callable[[], Awaitable[Reply]]:
    """Several actions in one phrase are taken back in reverse order."""
    if len(undos) == 1:
        return undos[0]

    async def run() -> Reply:
        said = [r.speech for r in [await u() for u in reversed(undos)] if r.speech]
        return Reply(" ".join(said) or "Отменил.")

    return run

ASK_INTERNET = Tool("ask_internet", "Найти свежую информацию в интернете и ответить на фактический вопрос.",
                    {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
                    "ask")


async def _once(text: str) -> AsyncIterator[str]:
    yield text


class Dialog:
    """Short rolling context, forgotten after a pause."""

    def __init__(self, ttl_sec: float, max_turns: int) -> None:
        self.ttl = ttl_sec
        # (time, role, content, is_action). Action turns are hidden from the LLM: a history of
        # "set a timer" -> "Timer set" without tool calls teaches the model to fake actions.
        self.turns: deque[tuple[float, str, str, bool]] = deque(maxlen=max_turns * 2)

    def _expire(self) -> None:
        if self.turns and time.monotonic() - self.turns[-1][0] > self.ttl:
            self.turns.clear()

    def add(self, role: str, content: str, action: bool = False) -> None:
        self._expire()
        if content:
            self.turns.append((time.monotonic(), role, content, action))

    def mark_last_user_action(self) -> None:
        for i in range(len(self.turns) - 1, -1, -1):
            t, role, content, _ = self.turns[i]
            if role == "user":
                self.turns[i] = (t, role, content, True)
                return

    def messages(self) -> list[Msg]:
        self._expire()
        return [Msg(role, content) for _, role, content, action in self.turns if not action]

    def last_topic(self) -> str:
        self._expire()
        users = [c for _, r, c, _ in self.turns if r == "user"]
        return users[-2] if len(users) >= 2 else ""


def is_info_question(text: str) -> bool:
    low = normalize_command(text)
    return not _SMALLTALK.search(low) and bool(_INFO.search(low))


class Brain:
    def __init__(self, app: "Assistant", skills: list[Skill]) -> None:
        self.app = app
        self.skills = skills
        # Exclusive skill calls requested by the local model, run after its speech ends.
        self.deferred: list[tuple[Skill, Intent]] = []
        self._turn_used_tools = False
        # A dangerous action waiting for "да": (callback, deadline).
        self.pending: tuple | None = None
        # A clarifying question waiting for an answer ("Chrome или Яндекс Браузер?"): (handler, deadline).
        self.asking: tuple | None = None
        # "не то" / "верни как было": how to take back the last action, and how its phrase was understood.
        self.last_undo: Callable[[], Awaitable[Reply]] | None = None
        self.last_route: tuple[str, str] | None = None     # (normalized phrase, skill | phrasebook | router)
        self.last_answer_key = ""                            # cache key of the last cloud answer
        # Last few requests and what was done: context for the router and "ещё раз".
        self.turns: deque[Turn] = deque(maxlen=6)
        self.last_command: tuple[Skill, Intent] | None = None
        # Replies of a compound command that must be spoken after the main one.
        self.extra: list[Reply] = []
        self.router = Router(app)
        self.tools: dict[str, tuple[Skill | None, Tool]] = {ASK_INTERNET.name: (None, ASK_INTERNET)}
        for skill in skills:
            for tool in skill.tools():
                self.tools[tool.name] = (skill, tool)

    def _system(self, addon: str) -> Msg:
        cfg = self.app.cfg.assistant
        now = dt.datetime.now()
        facts = self.user_facts()
        facts_text = ("\nЧто ты знаешь о пользователе (учитывай, но не пересказывай без повода):\n- " + "\n- ".join(facts)
                      if facts else "")
        return Msg("system", BASE_PROMPT.format(name=cfg.name, address=cfg.address, now=now.strftime("%d.%m.%Y %H:%M"),
                                                weekday=_WEEKDAYS[now.weekday()], city=cfg.default_city,
                                                facts=facts_text, screen=SCREEN_ADDON) + "\n" + addon)

    def user_facts(self) -> list[str]:
        memory = next((s for s in self.skills if s.name == "memory"), None)
        return memory.facts() if memory is not None else []  # type: ignore[attr-defined]

    def match_skill(self, text: str) -> tuple[Skill, Intent] | None:
        norm = normalize_command(text)
        for skill in self.skills:
            try:
                intent = skill.match(norm)
            except Exception:
                log.exception("match() в модуле %s", skill.name)
                continue
            if intent:
                intent.text = intent.text or norm
                intent.raw = text
                return skill, intent
        return None

    def _remember_confirm(self, reply: Reply) -> None:
        if reply.confirm is not None:
            self.pending = (reply.confirm, time.monotonic() + CONFIRM_TTL_SEC)
        if reply.ask is not None:
            self.asking = (reply.ask, time.monotonic() + CONFIRM_TTL_SEC)

    def expects_answer(self) -> bool:
        """Jarvis has just asked something: the next short "да" / "хром" must not be filtered out."""
        now = time.monotonic()
        return any(p is not None and now < p[1] for p in (self.pending, self.asking))

    async def _resolve_ask(self, text: str) -> Reply | None:
        if self.asking is None:
            return None
        handler, deadline = self.asking
        self.asking = None
        if time.monotonic() > deadline:
            return None
        norm = normalize_command(text, strip_polite=False)
        if NO.match(norm):
            return Reply("Хорошо, не буду.", listen_after=False)
        reply = await handler(norm)
        if reply is not None:
            self._remember_confirm(reply)
        return reply

    async def undo_last(self, forget: bool) -> Reply:
        """"верни как было" (forget=False) and "не то" (forget=True: the phrase was misunderstood)."""
        forgot = False
        if forget and self.last_route and self.last_route[1] in ("phrasebook", "router"):
            forgot = self.router.forget(self.last_route[0])
            self.router.drop_candidate(self.last_route[0])
            log.info("Забываю формулировку «%s»", private(self.last_route[0]))
        if forget and self.last_answer_key:
            self.router.forget_answer(self.last_answer_key)
            self.last_answer_key = ""
        undo, self.last_undo = self.last_undo, None
        if undo is not None:
            reply = await undo()
            log.info("Отменено: %s", reply.speech or "-")
            tail = " Эту формулировку я забыл, скажите иначе." if forgot else ""
            return Reply((reply.speech or "Отменил.") + tail, listen_after=forget)
        if forgot:
            return Reply("Понял, эту формулировку забыл. Скажите иначе.")
        if forget:
            return Reply("Уточните, что вы имели в виду.")
        return Reply("Здесь нечего отменять.", listen_after=False)

    async def _resolve_pending(self, text: str) -> Reply | None:
        """Answers a pending "are you sure?" question; None if the phrase is something else."""
        if self.pending is None:
            return None
        action, deadline = self.pending
        self.pending = None
        if time.monotonic() > deadline:
            return None
        norm = normalize_command(text, strip_polite=False)
        if YES.match(norm):
            log.info("Подтверждено")
            return await action()
        if NO.match(norm):
            return Reply("Отменил.", listen_after=False)
        return None

    # ------------------------------------------------------------------ main entry
    async def handle(self, text: str, addressed: bool = True) -> Reply:
        """regex modules -> compound split -> learned phrases -> model router -> answer. Streams are spoken by the caller.
        addressed=False: the phrase came without the name (hot window), the router may decide it was not for Jarvis."""
        confirmed = await self._resolve_pending(text)
        if confirmed is not None:
            self._note(text, confirmed.speech or "подтверждено")
            return confirmed
        answered = await self._resolve_ask(text)
        if answered is not None:
            self._note(text, answered.speech or "уточнено")
            return answered
        norm = normalize_command(text)

        # Compound first: otherwise "закрой браузер и включи музыку" becomes one "закрой <...>".
        parts = split_compound(norm)
        if len(parts) > 1:
            hits = [self.match_skill(p) for p in parts]
            if all(hits):
                log.info("Составная команда: %s", private(parts))
                return await self._run_commands(text, hits)  # type: ignore[arg-type]

        fallback: Reply | None = None
        decision = None
        hit = self.match_skill(text)
        if _ELLIPSIS.search(norm) and not (hit and hit[1].action.endswith("_many")):
            # "поставь будильник на 7 и другой на 8", "погода в Москве и в Питере": a module would serve only the
            # first half. The model splits it into commands; one command or a question -> the usual path.
            decision = await self.router.route(text, list(self.turns), addressed=addressed)
            if decision and decision.kind == "commands" and len(decision.commands) > 1:
                hits = [self.match_skill(c) for c in decision.commands]
                if all(hits):
                    log.info("Несколько действий в одной фразе: «%s» → %s", private(norm), decision.commands)
                    return await self._run_commands(text, hits, "router")  # type: ignore[arg-type]
        if hit:
            reply = await self._run_commands(text, [hit])
            if not reply.fallthrough:
                return reply
            log.info("Модуль %s не справился, спрашиваю маршрутизатор", hit[0].name)
            fallback = reply

        # Short follow-ups to the previous command ("а завтра?", "а в Сочи?") are resolved by its own skill.
        if self.last_command is not None and len(norm.split()) <= 5:
            skill, last = self.last_command
            follow = skill.followup(norm, last)
            if follow is not None:
                follow.text, follow.raw = norm, text
                log.info("Уточнение к %s.%s: %s", skill.name, follow.action, private(follow.slots))
                return await self._run_commands(text, [(skill, follow)])

        learned = self.router.learned(norm)
        if learned:
            hits = [self.match_skill(c) for c in learned]
            if all(hits):
                log.info("Выученная формулировка → %s", learned)
                return await self._run_commands(text, hits, "phrasebook")  # type: ignore[arg-type]
            self.router.forget(norm)

        if decision is None:
            decision = await self.router.route(text, list(self.turns), addressed=addressed)
        if decision and decision.kind == "ignore" and fallback is None:
            if not addressed:
                log.info("Похоже, это сказано не мне — молчу")
                return Reply(spoken=True, listen_after=False)
            return Reply("Не расслышал, повторите.")
        if decision and decision.kind == "commands":
            hits = [self.match_skill(c) for c in decision.commands]
            if all(hits):
                log.info("Маршрутизатор: «%s» → %s", private(norm), decision.commands)
                if not decision.context:
                    self.router.learn(norm, decision.commands)
                return await self._run_commands(text, hits, "router")  # type: ignore[arg-type]
            log.info("Маршрутизатор предложил неизвестные команды %s, отвечаю как на вопрос", decision.commands)
            decision = None
        if fallback is not None:
            return fallback

        if decision is None or decision.kind == "ignore":
            kind, query, fresh = ("web" if is_info_question(text) else "chat"), text, True
        else:
            kind, query, fresh = decision.kind, decision.query or text, decision.fresh
        self.last_undo, self.last_route = None, (norm, "answer")
        shown = self.app.show_visual_for(text)  # "что значит двойная связь": a molecule picture next to the answer
        picture = shown.kind if shown is not None else ""
        if kind == "web":
            log.info("Маршрут: облако (%s)", private(query))
            return self._answer_web(text, query, fresh, picture)
        log.info("Маршрут: локальная модель")
        history = self.app.dialog.messages()
        self.app.dialog.add("user", text)
        return Reply(stream=self._record(text, self._local_agent(history, text, picture)))

    # ------------------------------------------------------------------ commands
    async def _run_commands(self, text: str, hits: list[tuple[Skill, Intent]], source: str = "skill") -> Reply:
        replies: list[Reply] = []
        meta = all(skill.name == "system" and intent.action in _META for skill, intent in hits)
        for skill, intent in hits:
            log.info("Модуль %s.%s %s", skill.name, intent.action, private(intent.slots or ""))
            reply = await skill.handle(intent)
            self._remember_confirm(reply)
            replies.append(reply)
            if not (skill.name == "system" and intent.action in _META):
                self.last_command = (skill, intent)
        if not meta:
            undos = [r.undo for r in replies if r.undo is not None]
            self.last_undo = _chain_undo(undos) if undos else None
            self.last_route = (normalize_command(text), source)
            self.last_answer_key = ""
        self.app.dialog.add("user", text, action=True)
        said = " ".join(r.speech for r in replies if r.speech)
        if said:
            self.app.dialog.add("assistant", said, action=True)
        done = ", ".join(i.text for _, i in hits)
        self._note(text, f"выполнено «{done}»" + (f": {said[:90]}" if said else ""))
        if len(replies) == 1:
            return replies[0]
        # Several commands: all actions are done; speak the informative parts, streams follow.
        self.extra.extend(r for r in replies if r.stream is not None or r.spoken)
        speech = " ".join(r.speech for r in replies if r.speech and not r.reaction and r.stream is None and not r.spoken)
        return Reply(speech=speech, reaction="" if speech else ("ok" if any(r.reaction for r in replies) else ""),
                     listen_after=any(r.listen_after for r in replies),
                     confirm=next((r.confirm for r in replies if r.confirm), None))

    async def redo(self) -> Reply:
        """Ещё раз: repeat the last command; if there was none, repeat the last answer."""
        if self.last_command is not None:
            skill, intent = self.last_command
            log.info("Ещё раз: %s.%s", skill.name, intent.action)
            reply = await skill.handle(intent)
            self._remember_confirm(reply)
            return reply
        return Reply(self.app.speaker.last_text or "Пока нечего повторять.")

    def _note(self, text: str, did: str) -> None:
        self.turns.append(Turn(text, did))

    # ------------------------------------------------------------------ answers
    def answer(self, text: str, fresh: bool = False, picture: str = "") -> Reply:
        """A spoken answer to `text` from the cloud chain (skills that need an explanation, not an action)."""
        self.last_undo, self.last_route = None, (normalize_command(text), "answer")
        return self._answer_web(text, text, fresh, picture)

    @staticmethod
    def _picture_addon(picture: str) -> str:
        return ("\n" + PICTURE_ADDON[picture] + PICTURE_TAIL) if picture in PICTURE_ADDON else ""

    def _answer_web(self, text: str, query: str, fresh: bool, picture: str = "") -> Reply:
        # An answer written next to a picture is different (no drawing in text): it has its own cache entry.
        key = normalize_command(query) + (f" #{picture}" if picture else "")
        cached = self.router.cached_answer(key)
        history = self.app.dialog.messages()
        self.app.dialog.add("user", text)
        self.last_answer_key = key
        if cached:
            log.info("Ответ из кэша (облако не тратится)")
            self.app.llm.last_provider = "кэш"
            return Reply(stream=self._record(text, _once(cached)))
        # The router already folded the context into `query`; old turns would only cost tokens.
        keep = 0 if query != text else self.app.cfg.llm.cloud_history_turns * 2
        messages = [self._system(INFO_ADDON + self._picture_addon(picture)), *(history[-keep:] if keep else []),
                    Msg("user", query)]
        # Web search costs ~4k input tokens per question (search results go into the prompt):
        # only fresh data needs it; stable facts the model already knows.
        stream = self.app.llm.stream(self.app.cfg.llm.info_chain, messages, web=fresh)
        return Reply(stream=self._record(text, stream, cache_key=key, fresh=fresh))

    async def _record(self, text: str, stream: AsyncIterator[str], cache_key: str = "",
                      fresh: bool = True) -> AsyncIterator[str]:
        parts: list[str] = []
        self._turn_used_tools = False
        try:
            async for delta in stream:
                parts.append(delta)
                yield delta
        finally:
            answer = "".join(parts).strip()
            if self._turn_used_tools:
                self.app.dialog.mark_last_user_action()
            self.app.dialog.add("assistant", answer, action=self._turn_used_tools)
            self._note(text, f"ответил: {answer[:100]}")
            if cache_key and answer and self.app.llm.last_provider in ("groq", "gemini") and answer != FAIL_TEXT:
                self.router.remember_answer(cache_key, answer, fresh)

    async def _local_agent(self, history: list[Msg], text: str, picture: str = "") -> AsyncIterator[str]:
        """Local model with tool calling. Direct tools answer the user themselves."""
        local = self.app.llm.local
        self.app.llm.last_provider = "local"
        messages = [self._system(LOCAL_ADDON + self._picture_addon(picture)), *history, Msg("user", text)]
        schemas = [tool.schema() for _, tool in self.tools.values()]
        for _round in range(3):
            turn = LocalTurn()
            try:
                async for delta in local.stream_with_tools(messages, schemas, turn, max_tokens=1000):
                    yield delta
            except Exception as exc:
                log.warning("Локальная модель: %s", exc)
                # Cloud fallback gets only the conversation, never tool results (file contents, cmd output).
                safe = [self._system(""), *history, Msg("user", text)]
                async for delta in self.app.llm.stream(self.app.cfg.llm.info_chain, safe, web=False):
                    yield delta
                return
            if not turn.tool_calls:
                return
            self._turn_used_tools = True
            messages.append(Msg("assistant", turn.text, tool_calls=turn.tool_calls))
            direct_done = False
            for call in turn.tool_calls:
                name = call["function"]["name"]
                args = call["function"].get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                log.info("Инструмент %s %s", name, private(args))
                if name == ASK_INTERNET.name:
                    q = str(args.get("question") or text)
                    ask = [self._system(INFO_ADDON), Msg("user", q)]
                    async for delta in self.app.llm.stream(self.app.cfg.llm.info_chain, ask, web=True):
                        yield delta
                    direct_done = True
                    continue
                if name not in self.tools:
                    messages.append(Msg("tool", f"Нет инструмента {name}", tool_name=name))
                    continue
                skill, tool = self.tools[name]
                assert skill is not None
                intent = Intent(skill.name, tool.action, dict(args), text)
                if tool.exclusive:
                    self.deferred.append((skill, intent))
                    direct_done = True
                    continue
                intent.text = normalize_command(text)
                intent.raw = text
                reply = await skill.handle(intent)
                self._remember_confirm(reply)
                if reply.speech and tool.direct:
                    yield (" " if turn.text else "") + reply.speech
                if reply.stream:
                    async for delta in reply.stream:
                        yield delta
                messages.append(Msg("tool", reply.tool_result or reply.speech or "готово", tool_name=name))
                direct_done = direct_done or tool.direct
            if direct_done:
                return
