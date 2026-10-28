"""Phrase -> module routing with all modules in production order (the first match wins)."""
import asyncio

import pytest

from assistant.brain import Brain
from assistant.config import load_settings
from assistant.core import ConsoleUi
from assistant.skills.base import load_skills
from assistant.voicepack import load_pack


class FakeSpeaker:
    last_text = ""
    current_text = ""

    async def say(self, text):
        return text


class FakeApp:
    def __init__(self):
        self.cfg = load_settings()
        self.ui = ConsoleUi()
        self.speaker = FakeSpeaker()
        self.pack = load_pack("jarvis-remaster")
        self.interrupted = False
        self.announced = []
        self.mic = None
        self.music = type("Music", (), {"active": False, "state": "stopped", "current": None})()

    def interrupt(self):
        self.interrupted = True

    async def announce(self, text, cue=None):
        self.announced.append(text)

    def show_visual_for(self, text):
        self.visual_asked = text


@pytest.fixture(scope="module")
def app():
    a = FakeApp()
    a.skills = load_skills(a.cfg.skills.enabled)
    for s in a.skills:
        s.setup(a)
    music = next(s for s in a.skills if s.name == "music")
    music.tracks = lambda: {"i can be more": "C:/music/I can be more.mp3", "believer": "C:/music/Believer.mp3"}
    a.brain = Brain(a, a.skills)
    return a


def route(app, phrase):
    hit = app.brain.match_skill(phrase)
    return (hit[0].name, hit[1].action) if hit else None


@pytest.mark.parametrize("phrase", [
    "что такое блокчейн", "как дела", "расскажи шутку про программистов", "мне скучно", "кто написал войну и мир",
    "какой курс доллара", "мне нужно через полчаса проверить духовку", "почему небо голубое",
])
def test_left_for_the_model(app, phrase):
    assert route(app, phrase) is None, phrase


def test_timer_label(app):
    hit = app.brain.match_skill("напомни через пятнадцать минут выключить плиту")
    assert hit[1].slots == {"seconds": 900, "label": "выключить плиту"}


def test_weather_city_and_day(app):
    hit = app.brain.match_skill("какая погода будет завтра в нижнем новгороде")
    assert hit[1].slots == {"city": "нижнем новгороде", "day": 1, "part": ""}
    hit = app.brain.match_skill("погода в казани послезавтра")
    assert hit[1].slots == {"city": "казани", "day": 2, "part": ""}


def test_search_query_and_engine(app):
    assert app.brain.match_skill("найди в яндексе рецепт борща")[1].slots == {"q": "рецепт борща", "engine": "yandex"}
    assert app.brain.match_skill("найди на ютубе обзор 4060")[1].slots["engine"] == "youtube"
    assert app.brain.match_skill("загугли как почистить кэш")[1].slots == {"q": "как почистить кэш", "engine": "google"}


def test_timer_cancel_latest_only(app, tmp_path, monkeypatch):
    import assistant.skills.timers.main as timers_mod

    monkeypatch.setattr(timers_mod, "FILE", tmp_path / "timers.json")
    t = next(s for s in app.skills if s.name == "timers")

    async def run():
        await t.handle(t.match("поставь таймер на 10 минут"))
        await asyncio.sleep(0.01)
        await t.handle(t.match("напомни через 20 минут позвонить"))
        reply = await t.handle(t.match("убери таймер"))
        assert "позвонить" in reply.speech and len(t.timers) == 1
        reply = await t.handle(t.match("удали все таймеры"))
        assert t.timers == {}
        assert "отмен" in reply.speech.lower()

    asyncio.run(run())


def test_timer_fires(app, tmp_path, monkeypatch):
    import assistant.skills.timers.main as timers_mod

    monkeypatch.setattr(timers_mod, "FILE", tmp_path / "timers.json")
    t = next(s for s in app.skills if s.name == "timers")

    async def run():
        reply = await t.handle(t.match("таймер на 1 секунду"))
        assert "1 секунду" in reply.speech
        await asyncio.sleep(1.3)
        assert app.announced[-1] == "Время вышло, таймер сработал."

    asyncio.run(run())


def test_notes_roundtrip(app, tmp_path):
    notes = next(s for s in app.skills if s.name == "notes")
    app.cfg.notes.dir = str(tmp_path)

    async def run():
        await notes.handle(app.brain.match_skill("добавь в список покупок молоко, хлеб и яйца")[1])
        assert notes.items("покупки") == ["молоко", "хлеб", "яйца"]
        reply = await notes.handle(app.brain.match_skill("что в списке покупок")[1])
        assert "3 пункта" in reply.speech
        hit = app.brain.match_skill("вычеркни хлеб")
        assert hit[0].name == "notes"
        await notes.handle(hit[1])
        assert notes.items("покупки") == ["молоко", "яйца"]
        await notes.handle(app.brain.match_skill("молоко купил")[1])
        assert notes.items("покупки") == ["яйца"]
        reply = await notes.handle(app.brain.match_skill("очисти список покупок")[1])
        assert reply.confirm is not None
        await reply.confirm()
        assert notes.items("покупки") == []

    asyncio.run(run())


def test_confirmation_flow(app, tmp_path):
    notes = next(s for s in app.skills if s.name == "notes")
    app.cfg.notes.dir = str(tmp_path)
    notes.add("дела", ["позвонить"])

    async def run():
        app.dialog = type("D", (), {"add": lambda *a, **k: None, "messages": lambda self: [], "mark_last_user_action": lambda self: None})()
        reply = await app.brain.handle("очисти список дел")
        assert reply.confirm is not None and "Очистить" in reply.speech
        answer = await app.brain.handle("нет")
        assert answer.speech == "Отменил." and notes.items("дела") == ["позвонить"]
        await app.brain.handle("очисти список дел")
        answer = await app.brain.handle("да, давай")
        assert "очищен" in answer.speech and notes.items("дела") == []

    asyncio.run(run())


def test_memory_roundtrip(app, tmp_path, monkeypatch):
    import assistant.skills.memory.main as memory_mod

    monkeypatch.setattr(memory_mod, "FILE", tmp_path / "memory.json")
    mem = next(s for s in app.skills if s.name == "memory")
    mem._facts = []

    async def run():
        hit = app.brain.match_skill("Запомни, что я не ем мясо.")
        await mem.handle(hit[1])
        assert mem.facts() == ["я не ем мясо"]
        assert "я не ем мясо" in app.brain._system("").content
        await mem.handle(app.brain.match_skill("забудь что я не ем мясо")[1])
        assert mem.facts() == []

    asyncio.run(run())


def test_apps_resolve_alias(app):
    apps = next(s for s in app.skills if s.name == "apps")
    apps.index = {"telegram desktop": "shell:AppsFolder\\tg", "steam": "C:/steam.lnk"}
    assert apps.resolve("телеграм") == ("telegram desktop", "shell:AppsFolder\\tg")
    assert apps.resolve("стим") == ("steam", "C:/steam.lnk")
    assert apps.resolve("ютуб")[1] == "https://youtube.com"
    assert apps.resolve("habr.com") == ("habr.com", "https://habr.com")
    assert apps.resolve("несуществующее приложение xyz") is None


@pytest.mark.parametrize("follow,slots", [
    ("а завтра?", {"city": "казани", "day": 1}),
    ("а послезавтра", {"city": "казани", "day": 2}),
    ("а в Сочи?", {"city": "сочи", "day": 0}),
    ("а в Сочи завтра", {"city": "сочи", "day": 1}),
])
def test_weather_followups(app, follow, slots):
    from assistant.nlu import normalize_command

    weather = next(s for s in app.skills if s.name == "weather")
    last = app.brain.match_skill("какая погода в казани")[1]
    intent = weather.followup(normalize_command(follow), last)
    assert intent is not None and {k: v for k, v in intent.slots.items() if k != "part"} == slots


def test_weather_followup_ignores_other_phrases(app):
    from assistant.nlu import normalize_command

    weather = next(s for s in app.skills if s.name == "weather")
    last = app.brain.match_skill("какая погода в казани")[1]
    for phrase in ("а сколько ему лет", "открой телеграм", "спасибо"):
        assert weather.followup(normalize_command(phrase), last) is None


def test_calc_answers(app):
    calc = next(s for s in app.skills if s.name == "calc")

    def say(phrase):
        return asyncio.run(calc.handle(calc.match(phrase))).speech

    assert say("сколько будет 15% от 2300") == "345."
    assert say("сколько будет 7 разделить на 3") == "2,33."
    assert say("30 градусов цельсия в фаренгейтах") == "86 градусов Фаренгейта."
    assert say("10 фунтов в килограммы") == "4,54 килограмма."
    assert calc.match("какой курс доллара") is None and calc.match("сколько стоит биткоин") is None


def test_mood_playlists(app, tmp_path):
    music = next(s for s in app.skills if s.name == "music")
    for name in ("Calm", "Sad", "Phonk"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "track.mp3").write_bytes(b"")
    old = app.cfg.music.dir
    app.cfg.music.dir = str(tmp_path)
    try:
        assert music.playlist("спокойную").name == "Calm"
        assert music.playlist("что-нибудь грустное").name == "Sad"
        assert music.playlist("фонк").name == "Phonk"
        assert music.playlist("телеграм") is None
    finally:
        app.cfg.music.dir = old


SOLVED = r"""**Решение:**
1. Скорость сближения: $60 + 80 = 140$ км/ч.
2. Время: $t = \frac{420}{140} = 3$ ч.
* Значит, встретятся через 3 часа.

**Ответ:** $\boxed{3}$ часа."""


def test_solution_is_plain_text():
    from assistant.plaintext import split_solution

    solution, answer = split_solution(SOLVED)
    assert solution == "1. Скорость сближения: 60 + 80 = 140 км/ч.\n2. Время: t = 420/140 = 3 ч.\nЗначит, встретятся через 3 часа."
    assert answer == "3 часа"
    for text in (solution, answer):
        assert not any(ch in text for ch in "*#`$\\")


@pytest.mark.parametrize("raw,answer", [
    ("РЕШЕНИЕ:\nD = 25 - 24 = 1\nОТВЕТ:\n(2; 3)", "(2; 3)"),      # a point keeps its brackets
    ("Решение: x^2 = 4\nОтвет: [x = ±2].", "x = ±2"),
    ("РЕШЕНИЕ:\nВерный вариант Б\nОТВЕТ: «Б) Париж»", "Б) Париж"),
    ("x = 7 - 2\n5", "5"),                                         # no headers: the last line is the answer
])
def test_answer_is_bare(raw, answer):
    from assistant.plaintext import split_solution

    assert split_solution(raw)[1] == answer


def test_spoken_answer():
    from assistant.plaintext import spoken

    assert spoken("x² = 4; x = ±2") == "x в квадрате = 4, x = плюс минус 2"


def test_solve_shows_solution_and_answer_apart(app, monkeypatch):
    """"что на экране" then "реши эту задачу": the same region goes to the cloud, two pinned windows come back."""
    solve = next(s for s in app.skills if s.name == "solve")
    decks, sent = [], []

    async def complete(chain, messages, **kw):
        sent.append((chain, messages))
        return SOLVED

    monkeypatch.setattr(app, "llm", type("Llm", (), {"complete": staticmethod(complete), "last_provider": "gemini"})(),
                        raising=False)
    monkeypatch.setattr(app.ui, "show_deck", decks.append)
    solve.ctx.share("screen.region", b"png")   # what "что на экране" leaves behind

    async def run():
        reply = await solve.handle(solve.match("реши эту задачу"))
        return "".join([d async for d in reply.stream])

    speech = asyncio.run(run())
    assert sent[0][0] == ["gemini"] and sent[0][1][0].images == [b"png"]   # the cloud only, no local guesswork
    assert [d.title for d in decks] == ["Решение", "Ответ"] and all(d.pinned for d in decks)
    assert decks[1].cards[0].screen == "3 часа"
    assert speech == "Решаю. Ответ: 3 часа"

    # The local fallback, when allowed, is marked as possibly wrong.
    decks.clear()
    monkeypatch.setattr(solve.ctx.config, "local_fallback", True)
    monkeypatch.setattr(app.llm, "last_provider", "local")
    speech = asyncio.run(run())
    assert sent[1][0] == ["gemini", "local"]
    assert decks[1].title.startswith("Ответ · локально") and speech.startswith("Решаю. Облако недоступно")
