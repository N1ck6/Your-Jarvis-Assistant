"""Morning briefing: greeting, date, weather, calendar, timers, to-dos — one short monologue.

"доброе утро", "сводка", "что на сегодня"; optionally every day at [briefing] auto_time.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re

from assistant.core import Card, Deck
from assistant.nlu import plural
from assistant.skills.base import Intent, Reply, Skill

log = logging.getLogger("briefing")

_TRIGGER = re.compile(
    r"^(доброе утро|с добрым утром|утро доброе|доброе|утречко)( джарвис| сэр)?$|"
    r"^(утренняя |дневная |вечерняя )?(сводка|брифинг)( дня| на сегодня| на день)?$|^дай (мне )?сводку|^сводку$|"
    r"^что (у нас |у меня )?на сегодня$|^(какой |каков )?план на (сегодня|день)$|^что по плану( на сегодня)?$|"
    r"^введи (меня )?в курс( дела)?$|^что я пропустил$|^как (начнем|начинаем) день$"
)
_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def _greeting(address: str) -> str:
    h = dt.datetime.now().hour
    part = "Доброе утро" if 5 <= h < 12 else "Добрый день" if 12 <= h < 18 else "Добрый вечер" if 18 <= h < 23 else "Доброй ночи"
    return f"{part}, {address}."


class BriefingSkill(Skill):
    name = "briefing"
    title = "Утренняя сводка"

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def match(self, text: str) -> Intent | None:
        return Intent(self.name, "brief", text=text) if _TRIGGER.search(text) else None

    def _skill(self, name: str):
        return next((s for s in self.app.skills if s.name == name), None)

    async def compose(self) -> tuple[str, list[Card]]:
        now = dt.datetime.now()
        lines = [_greeting(self.app.cfg.assistant.address),
                 f"Сегодня {_WEEKDAYS[now.weekday()]}, {now.day} {_MONTHS[now.month - 1]}."]
        cards: list[Card] = []

        weather = self._skill("weather")
        if weather:
            try:
                from assistant.skills.base import Intent as I

                w = await weather.handle(I("weather", "forecast", {"city": "", "day": 0}))
                if w.speech and "не отвечает" not in w.speech:
                    lines.append(w.speech)
                    cards.append(Card("Погода", w.speech, ""))
            except Exception:
                log.exception("погода для сводки")

        agenda = self._skill("agenda")
        if agenda:
            text = await agenda.day_summary(0)
            if text:
                lines.append(text)
                cards.append(Card("Календарь", text, ""))

        timers = self._skill("timers")
        if timers and timers.timers:
            n = len(timers.timers)
            lines.append(f"Активных таймеров: {n}.")

        notes = self._skill("notes")
        if notes:
            todo = notes.items("дела")
            if todo:
                n = len(todo)
                lines.append(f"В делах {n} {plural(n, 'пункт', 'пункта', 'пунктов')}: " + ", ".join(todo[:3]) + ("…" if n > 3 else "."))
                cards.append(Card("Дела", "\n".join(f"- {t}" for t in todo[:8]), ""))
            buy = notes.items("покупки")
            if buy:
                n = len(buy)
                lines.append(f"В списке покупок {n} {plural(n, 'пункт', 'пункта', 'пунктов')}.")
        return " ".join(lines), cards

    async def handle(self, intent: Intent) -> Reply:
        text, cards = await self.compose()
        if cards:
            self.app.ui.show_deck(Deck(title="Сводка", cards=[Card("", "\n\n".join(f"{c.heading}: {c.screen}" for c in cards), "")],
                                       done=True))
        return Reply(text)

    async def start(self) -> None:
        if self.app.cfg.briefing.auto_time:
            self._task = asyncio.create_task(self._daily())

    async def _daily(self) -> None:
        try:
            hh, mm = (int(x) for x in self.app.cfg.briefing.auto_time.split(":"))
        except ValueError:
            log.warning("briefing.auto_time должен быть в формате ЧЧ:ММ")
            return
        while True:
            now = dt.datetime.now()
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= now:
                target += dt.timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            text, _ = await self.compose()
            await self.app.announce(text)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()


def create() -> Skill:
    return BriefingSkill()
