"""Clock times for alarms and reminders: "разбуди в 7", "завтра в 9 утра", "по будням в 7:30"."""
import asyncio
import datetime as dt

import pytest

from assistant.when import next_time, parse_when, say_when
from tests import test_skills

app = test_skills.app  # the skills fixture

NOW = dt.datetime(2026, 10, 1, 15, 0)  # Thursday, 15:00


@pytest.mark.parametrize("text,alarm,expected", [
    ("разбуди в 7", True, "02.10 07:00"),
    ("будильник на 6:30", True, "02.10 06:30"),
    ("разбуди без пятнадцати восемь", True, "02.10 07:45"),
    ("напомни в 7 позвонить маме", False, "01.10 19:00"),        # 7 at 15:00 is the evening
    ("напомни в 2 часа", False, "02.10 14:00"),                  # nobody plans reminders for 2 am
    ("напомни в два часа дня", False, "02.10 14:00"),
    ("напомни завтра в 9 утра", False, "02.10 09:00"),
    ("в пятницу в 18:00 напомни", False, "02.10 18:00"),
    ("в четверг в 14", False, "08.10 14:00"),                    # today's 14:00 has passed: next Thursday
    ("напомни в половине восьмого вечера", False, "01.10 19:30"),
    ("напомни в 16 часов 20 минут", False, "01.10 16:20"),
    ("напомни 5 октября в 12", False, "05.10 12:00"),
    ("напомни вечером", False, "01.10 19:00"),
])
def test_parse_when(text, alarm, expected):
    assert parse_when(text, NOW, alarm).at.strftime("%d.%m %H:%M") == expected


def test_durations_are_not_clock_times():
    assert parse_when("поставь таймер на 5 минут", NOW) is None
    assert parse_when("напомни через 10 минут", NOW) is None


def test_repeating():
    w = parse_when("будильник по будням в 7:30", NOW, alarm=True)
    assert w.repeat == "weekdays" and w.at.strftime("%a %H:%M") == "Fri 07:30"
    friday = dt.datetime(2026, 10, 2, 7, 30)
    assert next_time(friday, 7, 30, "weekdays").strftime("%a %d") == "Mon 05"   # skips the weekend
    assert parse_when("напоминай каждый понедельник в 10", NOW).repeat == "weekly:0"
    assert say_when(w.at, NOW, "weekdays") == "по будням в 7:30"
    assert say_when(dt.datetime(2026, 10, 2, 7, 0), NOW) == "завтра в 7:00"


@pytest.mark.parametrize("phrase,action", [
    ("разбуди меня в 7", "set_at"), ("поставь будильник на 6:30", "set_at"), ("будильник по будням в 7:30", "set_at"),
    ("напомни завтра в 9 утра про встречу", "set_at"), ("напомни в пятницу в 18:00 купить вино", "set_at"),
    ("напоминай каждый понедельник в 10 про отчёт", "set_at"),
    ("напомни через 10 минут выключить плиту", "set"), ("поставь таймер на 5 минут", "set"),
    ("отмени будильник", "cancel"),
])
def test_alarm_phrases(app, phrase, action):
    hit = app.brain.match_skill(phrase)
    assert hit and hit[0].name == "timers" and hit[1].action == action, phrase


def test_reminder_label_keeps_the_words(app, tmp_path, monkeypatch):
    import assistant.skills.timers as timers_mod

    monkeypatch.setattr(timers_mod, "FILE", tmp_path / "timers.json")
    hit = app.brain.match_skill("напомни завтра в 9 утра про встречу с Олегом")
    hit[1].raw = "Напомни завтра в 9 утра про встречу с Олегом."
    timers = hit[0]

    async def run():
        reply = await timers.handle(hit[1])
        for tid in list(timers.timers):
            timers._remove(tid)
        return reply

    assert asyncio.run(run()).speech.endswith("напомню: встречу с Олегом.")
