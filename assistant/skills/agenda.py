"""Calendar (read-only) from secret iCal links: Google Calendar, Outlook, Яндекс Календарь."""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

import httpx

from assistant.nlu import plural
from assistant.skills.base import Intent, Reply, Skill, Tool

log = logging.getLogger("agenda")

_EVENTS = r"(встреч\w*|созвон\w*|событи\w*|мероприяти\w*|планы|план|звонк\w*|совещани\w*|митинг\w*|расписани\w*)"
_TRIGGER = re.compile(
    rf"^что (у меня )?(сегодня|завтра|послезавтра|на неделе|на этой неделе)( по плану| в календаре)?$|"
    rf"(что|какие|какое|есть ли) (у меня )?(сегодня |завтра |послезавтра )?(еще )?(в календаре|по календарю|{_EVENTS})|"
    rf"^(покажи|открой|проверь|посмотри|глянь) (мой )?(календарь|расписание)|^календарь$|^расписание( на (сегодня|завтра|неделю))?$|"
    rf"^мое расписание|когда (у меня )?(следующ\w+|ближайш\w+) {_EVENTS}|^(я )?(свободен|занят) (ли )?(сегодня|завтра)"
)
_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}


def _to_local(value) -> tuple[dt.datetime, bool]:
    """(local naive datetime, all_day)."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value, False
    return dt.datetime.combine(value, dt.time()), True


class AgendaSkill(Skill):
    name = "agenda"
    title = "Календарь"
    examples = [
        'что у меня сегодня',
        'что у меня завтра',
        'когда следующая встреча',
    ]

    def __init__(self) -> None:
        self._cache: tuple[float, list[bytes]] | None = None

    def match(self, text: str) -> Intent | None:
        if not _TRIGGER.search(text):
            return None
        words = set(text.split())
        day = next((d for w, d in _DAYS.items() if w in words), 0)
        span = 7 if "неделе" in words or "неделю" in words else 1
        nxt = bool(re.search(r"следующ|ближайш", text))
        return Intent(self.name, "next" if nxt else "day", {"day": day, "span": span}, text)

    def tools(self) -> list[Tool]:
        return [Tool("calendar_events", "События из календаря пользователя на день (0 — сегодня, 1 — завтра).",
                     {"type": "object", "properties": {"day": {"type": "integer"}}}, "day")]

    async def _calendars(self) -> list[bytes]:
        urls = self.app.cfg.calendar.ics_urls
        ttl = self.app.cfg.calendar.cache_min * 60
        if self._cache and time.monotonic() - self._cache[0] < ttl:
            return self._cache[1]
        out = []
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for url in urls:
                try:
                    r = await client.get(url.replace("webcal://", "https://"))
                    r.raise_for_status()
                    out.append(r.content)
                except httpx.HTTPError as exc:
                    log.warning("Календарь %s: %s", url[:40], exc)
        self._cache = (time.monotonic(), out)
        return out

    async def events(self, start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, bool, str]] | None:
        """None = calendar not configured."""
        if not self.app.cfg.calendar.ics_urls:
            return None
        import icalendar
        import recurring_ical_events

        found = []
        for raw in await self._calendars():
            try:
                cal = icalendar.Calendar.from_ical(raw)
                for ev in recurring_ical_events.of(cal).between(start, end):
                    when, all_day = _to_local(ev.get("DTSTART").dt)
                    found.append((when, all_day, str(ev.get("SUMMARY", "Событие"))))
            except Exception:
                log.exception("Не разобрал календарь")
        return sorted(found, key=lambda e: e[0])

    async def day_summary(self, day: int = 0, span: int = 1) -> str | None:
        today = dt.datetime.combine(dt.date.today(), dt.time())
        start = today + dt.timedelta(days=day)
        events = await self.events(start, start + dt.timedelta(days=span))
        if events is None:
            return None
        when = {0: "Сегодня", 1: "Завтра", 2: "Послезавтра"}.get(day, "В эти дни") if span == 1 else "На неделе"
        if not events:
            return f"{when} в календаре пусто."
        parts = [(f"весь день — {t}" if all_day else f"в {w.hour}:{w.minute:02d} — {t}") if span == 1
                 else f"{w.day}.{w.month:02d} {'' if all_day else f'{w.hour}:{w.minute:02d} '}{t}"
                 for w, all_day, t in events[:8]]
        n = len(events)
        return f"{when} {n} {plural(n, 'событие', 'события', 'событий')}: " + "; ".join(parts) + "."

    async def handle(self, intent: Intent) -> Reply:
        if not self.app.cfg.calendar.ics_urls:
            return Reply("Календарь не подключён. Добавьте секретную ссылку iCal в настройки, раздел calendar.")
        if intent.action == "next":
            now = dt.datetime.now()
            events = [e for e in (await self.events(now, now + dt.timedelta(days=14)) or []) if not e[1] and e[0] >= now]
            if not events:
                return Reply("В ближайшие две недели встреч нет.")
            w, _, title = events[0]
            day = "сегодня" if w.date() == now.date() else "завтра" if w.date() == now.date() + dt.timedelta(days=1) else f"{w.day}.{w.month:02d}"
            return Reply(f"Следующее: {title}, {day} в {w.hour}:{w.minute:02d}.")
        text = await self.day_summary(int(intent.slots.get("day", 0)), int(intent.slots.get("span", 1)))
        return Reply(text or "Календарь недоступен.")


def create() -> Skill:
    return AgendaSkill()
