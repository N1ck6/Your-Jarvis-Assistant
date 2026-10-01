"""Clock times in Russian speech: "в 7 утра", "завтра в 19:30", "в пятницу в 18", "в половине восьмого",
"каждый будний день в 7:30". Used by alarms and timed reminders.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from assistant.nlu import words_to_numbers

_WEEKDAYS = {"понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3, "пятниц": 4, "суббот": 5, "воскресень": 6}
_ORD_GEN = {"первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5, "шестого": 6, "седьмого": 7,
            "восьмого": 8, "девятого": 9, "десятого": 10, "одиннадцатого": 11, "двенадцатого": 12}
_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
           "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_PART = r"(?P<part>утра|утром|дня|днем|вечера|вечером|ночи|ночью)"
_CLOCK = re.compile(
    rf"\b(?P<prep>в|на|к|около)\s+(?P<h>\d{{1,2}})(?:[:.\s](?P<m>\d{{2}})|\s+час\w*(?:\s+(?P<m2>\d{{1,2}})\s+минут\w*)?)?(?:\s+{_PART})?\b|"
    rf"\b(?:в|на)\s+половин[ае]\s+(?P<half>\w+ого)(?:\s+{_PART.replace('part', 'part2')})?\b|"
    rf"\b(?:в|на)\s+четверть\s+(?P<quarter>\w+ого)(?:\s+{_PART.replace('part', 'part3')})?\b|"
    rf"\bбез\s+(?P<to>\d{{1,2}})(?:\s+минут\w*)?\s+(?P<to_h>\d{{1,2}})(?:\s+{_PART.replace('part', 'part4')})?\b|"
    rf"\b(?P<alone>утром|вечером|днем)\b"
)
_REPEAT = [
    (re.compile(r"\b(кажд\w+ (день|утро|вечер)|ежедневно|всегда в)\b"), "daily"),
    (re.compile(r"\b(по будням|кажд\w+ будн\w+( день| дня)?|в будни|в будние дни|по рабочим дням)\b"), "weekdays"),
    (re.compile(r"\b(по выходным|в выходные|кажд\w+ выходн\w+)\b"), "weekends"),
]
_EVERY_DAY = re.compile(r"\b(?:кажд\w+|по)\s+(понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b")
_DAY = re.compile(r"\b(сегодня|завтра|послезавтра)\b")
_ON_WEEKDAY = re.compile(r"\bв(?:о)?\s+(?:эт\w+\s+|следующ\w+\s+|ближайш\w+\s+)?(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b")
_DATE = re.compile(rf"\b(\d{{1,2}})(?:-?го)?\s+({'|'.join(_MONTHS)})\b")
_DEFAULT_PART = {"утром": 8, "днем": 13, "вечером": 19}
# "без пятнадцати восемь": minutes in the genitive.
_GEN = {"пяти": "5", "десяти": "10", "пятнадцати": "15", "двадцати пяти": "25", "двадцати": "20", "четверти": "15"}


@dataclass
class When:
    at: dt.datetime
    repeat: str = ""        # "" | daily | weekdays | weekends | weekly:<0-6>
    spans: tuple[str, ...] = ()   # matched pieces, to cut out of the reminder label
    explicit_day: bool = False


def _weekday(word: str) -> int:
    return next(v for k, v in _WEEKDAYS.items() if word.startswith(k))


def _hour(h: int, part: str | None, now: dt.datetime, alarm: bool) -> int:
    if part in ("вечера", "вечером") and h < 12:
        return h + 12
    if part in ("дня", "днем") and h < 8:
        return h + 12
    if part in ("ночи", "ночью") and h == 12:
        return 0
    return h


def matches(at: dt.datetime, repeat: str) -> bool:
    wd = at.weekday()
    return (not repeat or repeat == "daily" or (repeat == "weekdays" and wd < 5) or (repeat == "weekends" and wd >= 5)
            or (repeat.startswith("weekly:") and wd == int(repeat[7:])))


def next_time(after: dt.datetime, hour: int, minute: int, repeat: str) -> dt.datetime:
    """The first moment after `after` at hh:mm on a day the repeat rule allows."""
    at = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= after:
        at += dt.timedelta(days=1)
    for _ in range(8):
        if matches(at, repeat):
            return at
        at += dt.timedelta(days=1)
    return at


def parse_when(text: str, now: dt.datetime | None = None, alarm: bool = False) -> When | None:
    """A clock time in the phrase -> the next moment it means. None if there is no clock time."""
    now = now or dt.datetime.now()
    low = text.lower().replace("ё", "е")
    low = re.sub(r"\bбез (" + "|".join(_GEN) + r")\b", lambda g: "без " + _GEN[g.group(1)], low)
    low = words_to_numbers(low)
    m = next((c for c in _CLOCK.finditer(low) if alarm or c.group("prep") != "на"), None)  # "на 5" is a duration
    if not m:
        return None
    spans = [m.group(0)]
    part = m.group("part") or m.group("part2") or m.group("part3") or m.group("part4")
    if m.group("h"):
        h, minute = int(m.group("h")), int(m.group("m") or m.group("m2") or 0)
    elif m.group("half"):
        h, minute = _ORD_GEN.get(m.group("half"), 0) - 1, 30
    elif m.group("quarter"):
        h, minute = _ORD_GEN.get(m.group("quarter"), 0) - 1, 15
    elif m.group("to"):
        h, minute = int(m.group("to_h")) - 1, 60 - int(m.group("to"))
    else:
        h, minute, part = _DEFAULT_PART[m.group("alone")], 0, None
    if not (0 <= h <= 24 and 0 <= minute < 60) or (m.group("half") or m.group("quarter")) and h < 0:
        return None
    h = _hour(h % 24 if h != 24 else 0, part, now, alarm)

    repeat = next((r for p, r in _REPEAT if p.search(low)), "")
    if e := _EVERY_DAY.search(low):
        repeat = f"weekly:{_weekday(e.group(1))}"
    for p in (pat for pat, _ in _REPEAT):
        if r := p.search(low):
            spans.append(r.group(0))
    if e:
        spans.append(e.group(0))

    explicit = False
    day = now.date()
    if d := _DAY.search(low):
        day = now.date() + dt.timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[d.group(1)])
        spans.append(d.group(0))
        explicit = True
    elif w := _ON_WEEKDAY.search(low):
        target = _weekday(w.group(1))
        ahead = (target - now.weekday()) % 7
        day = now.date() + dt.timedelta(days=ahead or 7 if "следующ" in w.group(0) else ahead)
        spans.append(w.group(0))
        explicit = True
    elif dm := _DATE.search(low):
        month, dnum = _MONTHS[dm.group(2)], int(dm.group(1))
        try:
            day = dt.date(now.year, month, dnum)
            if day < now.date():
                day = dt.date(now.year + 1, month, dnum)
        except ValueError:
            return None
        spans.append(dm.group(0))
        explicit = True

    if repeat:
        at = next_time(now, h, minute, repeat)
    else:
        at = dt.datetime.combine(day, dt.time(h, minute))
        if not explicit and not part and not alarm and 1 <= h < 12:
            # "напомни в 7" at 15:00 means 19:00 today; "в 2 часа" is 14:00, nobody plans reminders for 2 am.
            hours = [h + 12] if h <= 6 else [h, h + 12]
            at = min(next_time(now, x, minute, "") for x in hours)
        elif not explicit and at <= now:
            at += dt.timedelta(days=1)  # "разбуди в 7" in the evening is tomorrow morning
        elif explicit and at <= now and _DAY.search(low) is None:
            at += dt.timedelta(days=7)
    return When(at, repeat, tuple(spans), explicit)


def say_when(at: dt.datetime, now: dt.datetime | None = None, repeat: str = "") -> str:
    """'завтра в 7:00', 'в пятницу в 18:30', 'каждый будний день в 7:30'."""
    now = now or dt.datetime.now()
    clock = f"в {at.hour}:{at.minute:02d}"
    if repeat == "daily":
        return f"каждый день {clock}"
    if repeat == "weekdays":
        return f"по будням {clock}"
    if repeat == "weekends":
        return f"по выходным {clock}"
    days = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
    if repeat.startswith("weekly:"):
        every = {"понедельник": "каждый", "вторник": "каждый", "среду": "каждую", "четверг": "каждый",
                 "пятницу": "каждую", "субботу": "каждую", "воскресенье": "каждое"}
        name = days[int(repeat[7:])]
        return f"{every[name]} {name} {clock}"
    delta = (at.date() - now.date()).days
    if delta == 0:
        return f"сегодня {clock}"
    if delta == 1:
        return f"завтра {clock}"
    if delta == 2:
        return f"послезавтра {clock}"
    if delta < 7:
        prep = "во" if days[at.weekday()] == "вторник" else "в"
        return f"{prep} {days[at.weekday()]} {clock}"
    months = list(_MONTHS)
    return f"{at.day} {months[at.month - 1]} {clock}"
