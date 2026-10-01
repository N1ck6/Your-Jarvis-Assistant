"""Timers and reminders. Persisted in data/timers.json, survive restarts."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass

from assistant.audio import earcons
from assistant.nlu import parse_duration, plural, say_duration, sleep_until, words_to_numbers
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
)
_SET = re.compile(
    r"(таймер|напомни|напомнить|напоминани|напоминалк|разбуди|засеки|засечь|будильник|отсчет|отсчитай|"
    r"(скажи|сообщи|предупреди|дай знать|позови|крикни|маякни)( мне)? через|^через \d|^через (полчаса|полтора|пару|час)|"
    r"^(поставь|заведи|установи|запусти)( мне)? на )"
)
_LABEL_CLEAN = re.compile(
    r"\b(поставь|поставить|установи|заведи|запусти|засеки|засечь|таймер|напомни|напомнить|напоминание|напоминалку|"
    r"будильник|разбуди|скажи|сообщи|предупреди|дай знать|позови|крикни|маякни|мне|через|на|о том что|об этом|что|"
    r"чтобы|про|о|об|надо|нужно)\b",
)
_ALL = re.compile(r"\b(все|всё|оба|обе|таймеры|напоминания|будильники)\b")
_LAST = re.compile(r"\b(последний|последнее|этот|текущий)\b")


@dataclass
class Timer:
    id: str
    due: float       # unix time
    label: str
    created: float

    def left(self) -> int:
        return max(0, int(self.due - time.time()))


class TimersSkill(Skill):
    name = "timers"
    title = "Таймеры и напоминания"
    examples = [
        'поставь таймер на <N> минут',
        'напомни через <N> минут <что>',
        'какие у меня таймеры',
        'отмени таймер',
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
        if _SET.search(text):
            parsed = parse_duration(text)
            if parsed:
                seconds, span = parsed
                label = self._label(text.replace(span, " "))
                return Intent(self.name, "set", {"seconds": seconds, "label": label}, text)
        return None

    @staticmethod
    def _label(rest: str) -> str:
        rest = _LABEL_CLEAN.sub(" ", rest)
        rest = re.sub(r"\b\d+\b", " ", rest)
        return re.sub(r"\s+", " ", rest).strip(" ,.")

    def tools(self) -> list[Tool]:
        return [
            Tool("set_timer", "Поставить таймер или напоминание через заданное время.",
                 {"type": "object", "properties": {
                     "minutes": {"type": "number", "description": "Через сколько минут сработать"},
                     "label": {"type": "string", "description": "О чём напомнить (можно пусто)"}},
                  "required": ["minutes"]}, "set"),
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
            label = str(intent.slots.get("label") or "").strip()
            label = label[:1].lower() + label[1:]
            self._add(Timer(uuid.uuid4().hex[:8], time.time() + seconds, label, time.time()))
            if label:
                return Reply(f"Хорошо, через {say_duration(seconds)} напомню: {label}.", tool_result="таймер поставлен")
            return Reply(f"Поставил таймер на {say_duration(seconds)}.", tool_result="таймер поставлен")
        if intent.action == "list":
            if not self.timers:
                return Reply("Активных таймеров нет.")
            items = sorted(self.timers.values(), key=lambda t: t.due)
            parts = [f"{t.label or 'таймер'} через {say_duration(t.left())}" for t in items[:5]]
            n = len(items)
            return Reply(f"{n} {plural(n, 'таймер', 'таймера', 'таймеров')}: " + "; ".join(parts) + ".")
        if intent.action == "cancel":
            if not self.timers:
                return Reply("Активных таймеров нет.")
            text = intent.text or ""
            if _ALL.search(text) or len(self.timers) == 1 or intent.slots.get("all"):
                n = len(self.timers)
                for tid in list(self.timers):
                    self._remove(tid)
                return Reply("Таймер отменён." if n == 1 else f"Отменил {n} {plural(n, 'таймер', 'таймера', 'таймеров')}.")
            # Several timers, one asked: the most recently set one ("убери таймер" right after setting it).
            latest = list(self.timers.values())[-1]  # insertion order; time.time() ties on Windows
            self._remove(latest.id)
            rest = len(self.timers)
            what = f"«{latest.label}»" if latest.label else f"на {say_duration(int(latest.due - latest.created))}"
            return Reply(f"Отменил таймер {what}. Осталось ещё {rest}.")
        return Reply()

    def _add(self, timer: Timer) -> None:
        self.timers[timer.id] = timer
        self._tasks[timer.id] = asyncio.create_task(self._wait(timer))
        self._save()
        log.info("Таймер %s через %d с: %s", timer.id, timer.left(), timer.label)

    def _remove(self, tid: str) -> None:
        self.timers.pop(tid, None)
        task = self._tasks.pop(tid, None)
        if task and task is not asyncio.current_task():
            task.cancel()
        self._save()

    async def _wait(self, timer: Timer) -> None:
        await sleep_until(timer.due)
        self._remove(timer.id)
        text = f"Напоминаю: {timer.label}." if timer.label else "Время вышло, таймер сработал."
        self.app.ui.notify("Таймер", text)
        await self.app.announce(text, earcons.ALARM)

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
