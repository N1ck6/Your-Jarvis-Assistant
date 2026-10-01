"""Timers and reminders. Persisted in data/timers.json, survive restarts."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass

from assistant.audio import earcons
from assistant.log import private
from assistant.nlu import parse_duration, plural, say_duration, sleep_until, words_to_numbers
from assistant.when import next_time, parse_when, say_when
from assistant.paths import DATA_DIR
from assistant.skills.base import Intent, Reply, Skill, Tool

log = logging.getLogger("timers")
FILE = DATA_DIR / "timers.json"

_NOUN = r"(таймер\w*|напоминани\w*|напоминалк\w*|будильник\w*|отсчет\w*)"
_CANCEL_VERB = (r"(отмени\w*|отмена|удали\w*|убери|убрать|сбрось|сбросить|выключи\w*|отключи\w*|останови\w*|сними|снять|"
                r"очисти\w*|стоп|стопни|выруби\w*|забудь|отбой|отставить|хватит|прекрати|сотри)")
_QUALIFIER = r"((все|всё|мой|мои|этот|эти|последний|последние|текущий|текущие|старый|активные|все мои|оба|обе)\s+)*"
_CANCEL = re.compile(rf"\b{_CANCEL_VERB}\s+{_QUALIFIER}{_NOUN}|\b{_NOUN}\s+({_CANCEL_VERB}|не нужен|не нужны|не нужно|не надо|больше не нужен)\b")
_LIST = re.compile(
    rf"\b(какие|какой|сколько|список|покажи|покажите|мои|есть ли|что с|что по|проверь|статус|скажи|назови|перечисли|активные|напомни какие)\b.*\b{_NOUN}"
    rf"|^{_NOUN}( есть| какие| остались| активные)?$|^сколько (еще |еще времени |времени )?осталось"
    rf"|\bкогда (сработает|зазвенит|будет|прозвенит|закончится)\b"
    r"(?!.*\b(дожд|снег|гроз|ливен|ливн|осадк|морос|потепле|похолода|тепл|холод|мороз|солнц|жар|урок|пар|матч|игр|фильм))"
)
_SET = re.compile(
    r"(таймер|напомни|напомнить|напоминай|напоминани|напоминалк|разбуди|засеки|засечь|будильник|отсчет|отсчитай|"
    r"(скажи|сообщи|предупреди|дай знать|позови|крикни|маякни)( мне)? через|^через \d|^через (полчаса|полтора|пару|час)|"
    r"^(поставь|заведи|установи|запусти)( мне)? на )"
)
_LABEL_CLEAN = re.compile(
    r"\b(поставь|поставить|установи|заведи|запусти|засеки|засечь|таймер|напомни|напомнить|напоминание|напоминалку|"
    r"будильник|разбуди|скажи|сообщи|предупреди|дай знать|позови|крикни|маякни|мне|через|на|о том что|об этом|что|"
    r"чтобы|про|о|об|надо|нужно)\b",
)
_DURATIONS = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:час\w*|минут\w*|мин|секунд\w*|сек)\b|\b(?:час|минуту|секунду)\b")
# Several timers in one phrase: a separate one starts with "другой", "ещё один", "через", "на"...
_MARK = r"(?:другой|второй|третий|еще один|еще|таймер\w*|напомни|через|на)\b"
_SPLIT_MARKED = re.compile(rf"\s+(?:и|а|а также|и еще|а еще|плюс|потом|затем)\s+(?={_MARK})")
_SPLIT_ANY = re.compile(r"\s+(?:и|а|а также|и еще|а еще|плюс|потом|затем)\s+|\s*,\s*")
_SEVERAL = re.compile(r"\b(таймеры|два таймера|три таймера|2 таймера|3 таймера|несколько таймеров|напоминания)\b")
_UNIT = re.compile(r"\b(час(?:а|ов)?|минут(?:у|ы)?|секунд(?:у|ы)?)\b")
_EXTRA = re.compile(r"\b(другой|второй|третий|еще один|еще|один|таймеры|таймера|два|три)\b")
_ALL = re.compile(r"\b(все|всё|оба|обе|таймеры|напоминания|будильники)\b")
_LAST = re.compile(r"\b(последний|последнее|этот|текущий)\b")


@dataclass
class Timer:
    id: str
    due: float       # unix time
    label: str
    created: float
    repeat: str = ""     # "" | daily | weekdays | weekends | weekly:<0-6> (assistant/when.py)
    alarm: bool = False  # rings until "стоп" instead of one announcement

    def left(self) -> int:
        return max(0, int(self.due - time.time()))

    @property
    def clock(self) -> bool:
        """Set for a clock time ("в 7:30") rather than a duration ("через 10 минут")."""
        return self.alarm or bool(self.repeat) or self.label.startswith("@")


_ALARM = re.compile(r"\b(разбуди\w*|будильник\w*|подними меня|подъем)\b")
_CLOCK_HINT = re.compile(r"\b(в|к|около)\s+(\d|половин|четверть)|\bбез \S+ \d|\b(утром|вечером|днем)\b|"
                         r"\b(кажд\w+|по будням|по выходным|ежедневно)\b|\bзавтра\b|\bпослезавтра\b")


def _keep_case(label: str, raw: str) -> str:
    """Labels come from the normalized phrase; take the words as recognized: "встречу с Олегом"."""
    words = label.split()
    if not words or not raw:
        return label
    m = re.search(r"[\s,]+".join(re.escape(w).replace("е", "[её]") for w in words), raw, re.I)
    return m.group(0) if m else label


class TimersSkill(Skill):
    name = "timers"
    title = "Таймеры и напоминания"
    examples = [
        'поставь таймер на <N> минут',
        'напомни через <N> минут <что>',
        'напомни завтра в <ЧЧ:ММ> <что>',
        'разбуди в <ЧЧ:ММ>',
        'будильник по будням в <ЧЧ:ММ>',
        'какие у меня таймеры',
        'отмени таймер',
        'отмени будильник',
        'отмени все таймеры',
    ]

    def __init__(self) -> None:
        self.timers: dict[str, Timer] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # ---------------------------------------------------------------- matching
    def match(self, text: str) -> Intent | None:
        if _CANCEL.search(text):
            return Intent(self.name, "cancel", text=text)
        if _LIST.search(text):
            return Intent(self.name, "list", text=text)
        text = words_to_numbers(text)
        if not _SET.search(text):
            return None
        alarm = bool(_ALARM.search(text))
        # "через 10 минут" / "на 5 минут" is a duration; "в 7", "завтра в 9", "будильник на 6:30" a clock time.
        if (alarm or _CLOCK_HINT.search(text)) and not re.search(r"\bчерез\b", text):
            when = parse_when(text, alarm=alarm)
            if when:
                rest = text
                for piece in when.spans:
                    rest = rest.replace(piece, " ", 1)
                label = self._label(rest) if not alarm else ""
                return Intent(self.name, "set_at", {"due": when.at.timestamp(), "repeat": when.repeat,
                                                    "alarm": alarm, "label": label}, text)
        many = self._many(text)
        if many:
            return Intent(self.name, "set_many", {"items": many}, text)
        parsed = parse_duration(text)
        if parsed:
            seconds, span = parsed
            rest = text.replace(span, " ") if span in text else _DURATIONS.sub(" ", text)  # "1 час и 20 минут"
            label = self._label(rest)
            return Intent(self.name, "set", {"seconds": seconds, "label": label}, text)
        return None

    def _many(self, text: str) -> list[list] | None:
        """"таймер через минуту и другой через две", "таймеры на 5 и 10 минут", "напомни через 10 минут позвонить и
        через час выключить плиту" -> [[seconds, label], ...]. "на 1 час и 20 минут" stays one timer."""
        plural_noun = bool(_SEVERAL.search(text))
        parts = (_SPLIT_ANY if plural_noun else _SPLIT_MARKED).split(text)
        if len(parts) < 2:
            return None
        units = [_UNIT.findall(p) for p in parts]
        items: list[list] = []
        for i, part in enumerate(parts):
            parsed = parse_duration(part)
            if parsed is None and re.search(r"\d", part):
                # "...через минуту и другой через 2": the unit comes from a neighbour.
                borrowed = next((u[-1] for u in reversed(units[:i]) if u), None) or \
                    next((u[0] for u in units[i + 1:] if u), None)
                parsed = parse_duration(f"{part} {borrowed}") if borrowed else None
                if parsed:
                    parsed = (parsed[0], re.search(r"\d+(?:[.,]\d+)?", part).group(0))
            if parsed is None:
                return None
            seconds, span = parsed
            items.append([seconds, self._label(re.sub(_EXTRA, " ", part.replace(span, " ")))])
        return items

    @staticmethod
    def _label(rest: str) -> str:
        rest = re.sub(r"\b(утра|вечера|дня|ночи|утром|вечером|днем|ночью|сегодня|завтра|послезавтра|напоминай|"
                      r"каждый|каждую|каждое|по будням|по выходным)\b", " ", rest)
        rest = _LABEL_CLEAN.sub(" ", rest)
        rest = re.sub(r"\b\d+\b", " ", rest)
        rest = re.sub(r"\s+", " ", rest).strip(" ,.")
        return re.sub(r"^(и|а)\b\s*|\s*\b(и|а)$", "", rest).strip()

    def tools(self) -> list[Tool]:
        return [
            Tool("set_timer", "Поставить таймер или напоминание через заданное время.",
                 {"type": "object", "properties": {
                     "minutes": {"type": "number", "description": "Через сколько минут сработать"},
                     "label": {"type": "string", "description": "О чём напомнить (можно пусто)"}},
                  "required": ["minutes"]}, "set"),
            Tool("remind_at", "Напоминание или будильник на время по часам (не через N минут).",
                 {"type": "object", "properties": {
                     "when": {"type": "string", "description": "Когда, словами: «завтра в 9:00», «в пятницу в 18:30», "
                                                               "«по будням в 7:30»"},
                     "label": {"type": "string", "description": "О чём напомнить (пусто для будильника)"},
                     "alarm": {"type": "boolean", "description": "true — будильник"}},
                  "required": ["when"]}, "tool_at"),
            Tool("list_timers", "Показать активные таймеры.", {"type": "object", "properties": {}}, "list"),
            Tool("cancel_timers", "Отменить таймеры: все или только последний поставленный.",
                 {"type": "object", "properties": {"all": {"type": "boolean", "description": "true — все таймеры"}}},
                 "cancel"),
        ]

    # ---------------------------------------------------------------- actions
    async def handle(self, intent: Intent) -> Reply:
        if intent.action == "set":
            seconds = int(intent.slots.get("seconds") or float(intent.slots.get("minutes", 0)) * 60)
            if seconds <= 0:
                return Reply("Не понял, на сколько поставить таймер.")
            label = _keep_case(str(intent.slots.get("label") or "").strip(), intent.raw)
            label = label[:1].lower() + label[1:]
            timer = Timer(uuid.uuid4().hex[:8], time.time() + seconds, label, time.time())
            self._add(timer)
            undo = self._undo_set(timer)
            if label:
                return Reply(f"Хорошо, через {say_duration(seconds)} напомню: {label}.", tool_result="таймер поставлен",
                             undo=undo)
            return Reply(f"Поставил таймер на {say_duration(seconds)}.", tool_result="таймер поставлен", undo=undo)
        if intent.action == "set_many":
            made: list[Timer] = []
            for seconds, label in intent.slots["items"]:
                label = _keep_case(str(label).strip(), intent.raw)
                timer = Timer(uuid.uuid4().hex[:8], time.time() + int(seconds), label[:1].lower() + label[1:], time.time())
                self._add(timer)
                made.append(timer)
            said = [f"через {say_duration(int(t.due - t.created))}" + (f" — {t.label}" if t.label else "") for t in made]
            n = len(made)

            async def undo() -> Reply:
                for t in made:
                    self._remove(t.id)
                return Reply("Таймеры убрал.")
            return Reply(f"Поставил {n} {plural(n, 'таймер', 'таймера', 'таймеров')}: " + ", ".join(said) + ".",
                         tool_result="таймеры поставлены", undo=undo)
        if intent.action == "set_at":
            label = _keep_case(str(intent.slots.get("label") or ""), intent.raw)
            return self._set_at(float(intent.slots["due"]), label, str(intent.slots.get("repeat") or ""),
                                bool(intent.slots.get("alarm")))
        if intent.action == "tool_at":
            alarm = bool(intent.slots.get("alarm"))
            when = parse_when(str(intent.slots.get("when") or ""), alarm=alarm)
            if when is None:
                return Reply("Не понял время.", tool_result="время не распознано")
            return self._set_at(when.at.timestamp(), str(intent.slots.get("label") or ""), when.repeat, alarm)
        if intent.action == "list":
            if not self.timers:
                return Reply("Активных таймеров нет.")
            items = sorted(self.timers.values(), key=lambda t: t.due)
            parts = [self._describe(t) for t in items[:5]]
            n = len(items)
            return Reply(f"{n} {plural(n, 'таймер', 'таймера', 'таймеров')}: " + "; ".join(parts) + ".")
        if intent.action == "cancel":
            if not self.timers:
                return Reply("Активных таймеров нет.")
            text = intent.text or ""
            if re.search(r"\bбудильник", text) and any(t.alarm for t in self.timers.values()):
                alarms = [t for t in self.timers.values() if t.alarm]
                if not _ALL.search(text) and len(alarms) > 1:
                    alarms = alarms[-1:]
                for t in alarms:
                    self._remove(t.id)
                return Reply("Будильник отменён." if len(alarms) == 1 else f"Отменил будильники: {len(alarms)}.",
                             undo=self._undo_cancel(alarms))
            if _ALL.search(text) or len(self.timers) == 1 or intent.slots.get("all"):
                n = len(self.timers)
                cancelled = list(self.timers.values())
                for tid in list(self.timers):
                    self._remove(tid)
                return Reply("Таймер отменён." if n == 1 else f"Отменил {n} {plural(n, 'таймер', 'таймера', 'таймеров')}.",
                             undo=self._undo_cancel(cancelled))
            # Several timers, one asked: the most recently set one ("убери таймер" right after setting it).
            latest = list(self.timers.values())[-1]  # insertion order; time.time() ties on Windows
            self._remove(latest.id)
            rest = len(self.timers)
            what = f"«{latest.label}»" if latest.label else f"на {say_duration(int(latest.due - latest.created))}"
            return Reply(f"Отменил таймер {what}. Осталось ещё {rest}.", undo=self._undo_cancel([latest]))
        return Reply()

    def _set_at(self, due: float, label: str, repeat: str, alarm: bool) -> Reply:
        label = label[:1].lower() + label[1:]
        timer = Timer(uuid.uuid4().hex[:8], due, label, time.time(), repeat, alarm)
        self._add(timer)
        when = say_when(dt.datetime.fromtimestamp(due), repeat=repeat)
        if alarm:
            text = f"Будильник {when}."
        elif label:
            text = f"Хорошо, {when} напомню: {label}."
        else:
            text = f"Напомню {when}."
        return Reply(text, tool_result="поставлено", undo=self._undo_set(timer))

    @staticmethod
    def _describe(t: Timer) -> str:
        if t.alarm or t.repeat or t.left() > 3 * 3600:
            what = "будильник" if t.alarm else (t.label or "напоминание")
            return f"{what} {say_when(dt.datetime.fromtimestamp(t.due), repeat=t.repeat)}"
        left = t.left()
        left = -(-left // 60) * 60 if left >= 90 else left  # "через 10 минут", not "9 минут 54 секунды"
        return f"{t.label or 'таймер'} через {say_duration(left)}"

    def _undo_set(self, timer: Timer):
        async def undo() -> Reply:
            if timer.id in self.timers:
                self._remove(timer.id)
            return Reply("Таймер убрал.")
        return undo

    def _undo_cancel(self, cancelled: list[Timer]):
        async def undo() -> Reply:
            back = [t for t in cancelled if t.due > time.time()]
            for t in back:
                self._add(t)
            return Reply("Вернул таймер." if len(back) == 1 else f"Вернул таймеры: {len(back)}." if back else
                         "Эти таймеры уже истекли.")
        return undo

    def _add(self, timer: Timer) -> None:
        self.timers[timer.id] = timer
        self._tasks[timer.id] = asyncio.create_task(self._wait(timer))
        self._save()
        log.info("Таймер %s через %d с: %s", timer.id, timer.left(), private(timer.label))

    def _remove(self, tid: str) -> None:
        self.timers.pop(tid, None)
        task = self._tasks.pop(tid, None)
        if task and task is not asyncio.current_task():
            task.cancel()
        self._save()

    async def _wait(self, timer: Timer) -> None:
        await sleep_until(timer.due)
        self._remove(timer.id)
        if timer.repeat:  # the next day it applies to, at the same clock time
            due = dt.datetime.fromtimestamp(timer.due)
            nxt = next_time(max(due, dt.datetime.now()), due.hour, due.minute, timer.repeat)
            self._add(Timer(uuid.uuid4().hex[:8], nxt.timestamp(), timer.label, time.time(), timer.repeat, timer.alarm))
        if timer.alarm:
            await self._ring(timer)
            return
        text = f"Напоминаю: {timer.label}." if timer.label else "Время вышло, таймер сработал."
        self.app.ui.notify("Таймер", text)
        await self.app.announce(text, earcons.ALARM)

    async def _ring(self, timer: Timer) -> None:
        """An alarm repeats every 30 s (5 times) until anything is said to Jarvis: "стоп", "Джарвис", a command."""
        now = dt.datetime.now()
        text = f"Доброе утро, {self.app.cfg.assistant.address}. Сейчас {now.hour}:{now.minute:02d}." if now.hour < 12 else \
            f"{self.app.cfg.assistant.address.capitalize()}, сработал будильник. Сейчас {now.hour}:{now.minute:02d}."
        self.app.ui.notify("Будильник", f"{now.hour}:{now.minute:02d}")
        mark = self.app.interruptions
        for _ in range(5):
            await self.app.announce(text, earcons.ALARM)
            for _ in range(30):
                await asyncio.sleep(1)
                if self.app.interruptions != mark:
                    log.info("Будильник выключен")
                    return

    # ---------------------------------------------------------------- persistence
    def _save(self) -> None:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps([asdict(t) for t in self.timers.values()], ensure_ascii=False, indent=1),
                        encoding="utf-8")

    async def start(self) -> None:
        if not FILE.exists():
            return
        try:
            saved = [Timer(**d) for d in json.loads(FILE.read_text(encoding="utf-8"))]
            for t in saved:  # a repeating alarm missed while the PC was off: move it to its next time
                if t.repeat and t.due <= time.time():
                    due = dt.datetime.fromtimestamp(t.due)
                    t.due = next_time(dt.datetime.now(), due.hour, due.minute, t.repeat).timestamp()
        except (OSError, ValueError, TypeError):
            log.warning("timers.json повреждён, пропускаю")
            return
        missed = [t for t in saved if t.due <= time.time()]
        for t in saved:
            if t.due > time.time():
                self._add(t)
        self._save()
        if missed:
            labels = ", ".join(t.label or "таймер" for t in missed)
            self.app.ui.notify("Пропущенные напоминания", labels)
            asyncio.create_task(self.app.announce(f"Пока я был выключен, сработали напоминания: {labels}."))

    async def stop(self) -> None:
        for task in self._tasks.values():
            task.cancel()


def create() -> Skill:
    return TimersSkill()
