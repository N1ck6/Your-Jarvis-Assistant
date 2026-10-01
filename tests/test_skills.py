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


CASES = [
    # --- system
    ("стоп", "system", "stop"), ("Хватит.", "system", "stop"), ("замолчи", "system", "stop"), ("подожди", "system", "stop"),
    ("отмена", "system", "stop"), ("ладно, хватит", "system", "stop"),
    ("который час", "system", "time"), ("Сколько сейчас времени?", "system", "time"), ("подскажи время", "system", "time"),
    ("какое сегодня число", "system", "date"), ("какой сегодня день недели", "system", "date"),
    ("повтори", "system", "repeat"), ("не расслышал", "system", "repeat"), ("скажи ещё раз", "system", "repeat"),
    ("спасибо", "system", "thanks"), ("Спасибо большое!", "system", "thanks"), ("молодец", "system", "thanks"),
    ("ты тупой", "system", "insult"), ("пошути", "system", "joke"), ("расскажи анекдот", "system", "joke"),
    ("выключи микрофон", "system", "mute"), ("не слушай", "system", "mute"),
    ("что ты умеешь", "system", "help"), ("какие у тебя команды", "system", "help"),
    ("выключись", "system", "exit"), ("заверши работу", "system", "exit"),
    ("ещё раз", "system", "again"), ("снова", "system", "again"), ("сделай ещё раз", "system", "again"),
    ("расскажи мне шутку", "system", "joke"), ("открой, пожалуйста, телеграм", "apps", "open"),
    # --- music player
    ("что играет", "music", "now"), ("что за песня", "music", "now"),
    ("сколько композиций в папке музыка", "music", "count"), ("сколько треков", "music", "count"),
    ("сколько песен у меня в папке с музыкой", "music", "count"), ("выключи музыку", "music", "stop"),
    # --- computer & files (read-only)
    ("сколько места на диске", "pc", "disk"), ("сколько свободного места на компьютере", "pc", "disk"),
    ("сколько свободной оперативной памяти", "pc", "ram"), ("что грузит компьютер", "pc", "cpu"),
    ("почему тормозит компьютер", "pc", "cpu"), ("сколько работает компьютер", "pc", "uptime"),
    ("заряд батареи", "pc", "battery"), ("какой у меня ip", "pc", "ip"), ("какой mac адрес у моего компьютера", "pc", "mac"),
    ("сколько файлов в загрузках", "pc", "count"), ("сколько pdf в документах", "pc", "count"),
    ("сколько фото на рабочем столе", "pc", "count"), ("что в загрузках", "pc", "recent"),
    ("последние загрузки", "pc", "recent"), ("что я скачал", "pc", "recent"),
    ("сколько весит папка загрузки", "pc", "size"), ("найди файл отчет", "pc", "find"),
    ("где лежит договор", "pc", "find"), ("открой папку загрузки", "pc", "open_dir"),
    ("хватает ли мне места на компе", "pc", "disk"),
    # --- media (no "set volume to N")
    ("громче", "media", "vol_up"), ("сделай погромче", "media", "vol_up"), ("прибавь звук", "media", "vol_up"),
    ("увеличь громкость", "media", "vol_up"), ("тише", "media", "vol_down"), ("убавь", "media", "vol_down"),
    ("сделай потише пожалуйста", "media", "vol_down"),
    ("выключи звук", "media", "mute"), ("Срочно выключи звук!", "media", "mute"), ("убери звук", "media", "mute"),
    ("включи звук", "media", "unmute"), ("верни звук", "media", "unmute"),
    ("пауза", "media", "pause"), ("поставь на паузу", "media", "pause"), ("останови музыку", "media", "pause"),
    ("продолжи", "media", "resume"), ("сними с паузы", "media", "resume"),
    ("следующий трек", "media", "next"), ("следующая песня", "media", "next"), ("переключи трек", "media", "next"),
    ("предыдущий трек", "media", "prev"), ("верни прошлый трек", "media", "prev"),
    ("какая громкость", "media", "volume"),
    # --- music from the folder
    ("включи I can be more", "music", "play"), ("поставь believer", "music", "play"),
    ("включи песню believer", "music", "play"), ("включи музыку", "music", "shuffle"),
    ("хочу послушать I can be more", "music", "play"), ("включи что-нибудь", "music", "shuffle"),
    # --- dictation
    ("диктовка", "dictation", "start"), ("пиши под диктовку", "dictation", "start"), ("запиши текст", "dictation", "start"),
    ("напечатай привет, буду через 10 минут", "dictation", "type"),
    # --- selected text
    ("переведи", "selection", "translate"), ("переведи выделенное на английский", "selection", "translate"),
    ("переведи это", "selection", "translate"), ("объясни выделенное", "selection", "explain"),
    ("что это значит", "selection", "explain"), ("перескажи", "selection", "summary"),
    ("сократи этот текст", "selection", "summary"), ("исправь ошибки", "selection", "fix"),
    ("проверь орфографию", "selection", "fix"), ("перепиши вежливо", "selection", "rewrite"),
    ("прочитай выделенное", "selection", "read"), ("озвучь это", "selection", "read"),
    # --- screen
    ("что на экране", "screen", "help"), ("что у меня на экране?", "screen", "help"),
    ("посмотри на экран", "screen", "help"), ("помоги с этим", "screen", "help"),
    ("что тут написано", "screen", "help"), ("что это за ошибка", "screen", "help"),
    ("переведи текст на экране", "screen", "help"), ("сделай скриншот", "screen", "help"),
    # --- notes & lists
    ("добавь в список покупок молоко и хлеб", "notes", "add"), ("запиши в список дел позвонить врачу", "notes", "add"),
    ("купить молоко", "notes", "add"), ("надо купить батарейки", "notes", "add"),
    ("добавь молоко в список покупок", "notes", "add"), ("сделай заметку идея для проекта", "notes", "add"),
    ("запиши позвонить Саше вечером", "notes", "add"), ("новая задача оплатить интернет", "notes", "add"),
    ("что в списке покупок", "notes", "read"), ("прочитай список дел", "notes", "read"),
    ("какие у меня дела", "notes", "read"), ("что мне нужно купить", "notes", "read"), ("покажи заметки", "notes", "read"),
    ("очисти список покупок", "notes", "clear"),
    # --- windows
    ("закрой телеграм", "windows", "close"), ("закрой браузер", "windows", "close"), ("закрой это окно", "windows", "close"),
    ("выключи стим", "windows", "close"), ("сверни всё", "windows", "minimize_all"), ("покажи рабочий стол", "windows", "minimize_all"),
    ("сверни окно", "windows", "minimize"), ("разверни хром на весь экран", "windows", "maximize"),
    ("на весь экран", "windows", "maximize"), ("переключись на телеграм", "windows", "switch"),
    ("перейди в браузер", "windows", "switch"), ("какие окна открыты", "windows", "list"),
    ("убей процесс хром", "windows", "kill"),
    # --- search in the browser
    ("найди в яндексе рецепт борща", "search", "search"), ("найди в браузере курс python", "search", "search"),
    ("поищи в интернете новости спорта", "search", "search"), ("найди на ютубе обзор 4060", "search", "search"),
    ("загугли как почистить кэш", "search", "search"), ("найди рецепт борща в яндексе", "search", "search"),
    # --- timers
    ("поставь таймер на 5 минут", "timers", "set"), ("таймер на пять минут", "timers", "set"),
    ("засеки полчаса", "timers", "set"), ("напомни через 10 минут позвонить маме", "timers", "set"),
    ("через двадцать минут напомни выключить плиту", "timers", "set"), ("разбуди через час", "timers", "set"),
    ("скажи мне через 3 минуты", "timers", "set"), ("поставь на 7 минут", "timers", "set"),
    ("убери таймер", "timers", "cancel"), ("отмени таймер", "timers", "cancel"), ("удали все таймеры", "timers", "cancel"),
    ("сбрось таймер", "timers", "cancel"), ("таймер не нужен", "timers", "cancel"), ("выключи таймер", "timers", "cancel"),
    ("сними напоминание", "timers", "cancel"), ("отмени последний таймер", "timers", "cancel"),
    ("какие у меня таймеры", "timers", "list"), ("покажи таймеры", "timers", "list"), ("таймеры", "timers", "list"),
    ("сколько осталось", "timers", "list"), ("когда сработает таймер", "timers", "list"), ("мои напоминания", "timers", "list"),
    # --- pomodoro
    ("помодоро", "focus", "start"), ("запусти помодоро на 50 минут", "focus", "start"),
    ("останови помодоро", "focus", "stop"), ("сколько до перерыва", "focus", "status"),
    # --- memory
    ("запомни, что я не ем мясо", "memory", "remember"), ("учти что у меня аллергия на орехи", "memory", "remember"),
    ("что ты обо мне знаешь", "memory", "recall"), ("что ты запомнил", "memory", "recall"),
    ("забудь что я не ем мясо", "memory", "forget"), ("забудь всё обо мне", "memory", "forget_all"),
    # --- briefing & calendar
    ("доброе утро", "briefing", "brief"), ("Доброе утро, Джарвис!", "briefing", "brief"), ("сводка", "briefing", "brief"),
    ("что на сегодня", "briefing", "brief"), ("какой план на день", "briefing", "brief"),
    ("что у меня сегодня", "agenda", "day"), ("что у меня завтра", "agenda", "day"), ("какие у меня встречи", "agenda", "day"),
    ("есть ли созвоны завтра", "agenda", "day"), ("покажи календарь", "agenda", "day"),
    ("когда следующая встреча", "agenda", "next"),
    # --- apps
    ("открой телеграм", "apps", "open"), ("запусти стим", "apps", "open"), ("открой блокнот", "apps", "open"),
    ("включи дискорд", "apps", "open"), ("можешь открыть ютуб", "apps", "open"), ("зайди на habr.com", "apps", "open"),
    ("хочу поиграть в geometry dash", "apps", "open"),
    # --- weather
    ("какая погода", "weather", "forecast"), ("погода в Казани завтра", "weather", "forecast"),
    ("будет ли дождь", "weather", "forecast"), ("нужен ли зонт", "weather", "forecast"),
    ("что на улице", "weather", "forecast"), ("как одеться сегодня", "weather", "forecast"),
    ("сколько градусов", "weather", "forecast"),
    # --- explain window
    ("объясни подробно что такое DNS", "explain", "explain"), ("расскажи наглядно про чёрные дыры", "explain", "explain"),
    ("объясни что такое блокчейн по полочкам", "explain", "explain"), ("сделай презентацию про квантовые компьютеры", "explain", "explain"),
    ("подробнее", "explain", "explain"), ("разбери тему инфляция", "explain", "explain"),
    # --- voice
    ("смени голос", "voice", "lab"), ("открой настройки голоса", "voice", "lab"), ("говори быстрее", "voice", "rate"),
    ("ты говоришь слишком быстро", "voice", "rate"),
    # --- user scenarios (config/scenarios.example.toml)
    ("режим фокуса", "scenarios", "run"), ("включи фокус", "scenarios", "run"), ("хочу поработать", "scenarios", "run"),
    ("игровой режим", "scenarios", "run"),
]


@pytest.mark.parametrize("phrase,skill,action", CASES)
def test_routing(app, phrase, skill, action):
    assert route(app, phrase) == (skill, action), phrase


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
    assert hit[1].slots == {"city": "нижнем новгороде", "day": 1}
    hit = app.brain.match_skill("погода в казани послезавтра")
    assert hit[1].slots == {"city": "казани", "day": 2}


def test_search_query_and_engine(app):
    assert app.brain.match_skill("найди в яндексе рецепт борща")[1].slots == {"q": "рецепт борща", "engine": "yandex"}
    assert app.brain.match_skill("найди на ютубе обзор 4060")[1].slots["engine"] == "youtube"
    assert app.brain.match_skill("загугли как почистить кэш")[1].slots == {"q": "как почистить кэш", "engine": "google"}


def test_timer_cancel_latest_only(app, tmp_path, monkeypatch):
    import assistant.skills.timers as timers_mod

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
    import assistant.skills.timers as timers_mod

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
    import assistant.skills.memory as memory_mod

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
    assert intent is not None and intent.slots == slots


def test_weather_followup_ignores_other_phrases(app):
    from assistant.nlu import normalize_command

    weather = next(s for s in app.skills if s.name == "weather")
    last = app.brain.match_skill("какая погода в казани")[1]
    for phrase in ("а сколько ему лет", "открой телеграм", "спасибо"):
        assert weather.followup(normalize_command(phrase), last) is None
