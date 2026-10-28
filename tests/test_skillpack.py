"""Skills as folders: manifests, loading order, user skills, per-skill settings, the phrases each skill declares."""
import asyncio
import textwrap

import pytest

from assistant import config, skillpack
from assistant.brain import Brain
from assistant.skills.base import load_skills
from tests.test_skills import FakeApp

BUILTIN = skillpack.discover()
BUILTIN_IDS = [m.id for m in BUILTIN]


@pytest.fixture(scope="module")
def app():
    a = FakeApp()
    a.skills = load_skills(a.cfg.skills.enabled, a.cfg.skills.disabled)
    for s in a.skills:
        s.setup(a)
    music = next(s for s in a.skills if s.name == "music")   # the same songs as in tests/test_skills.py
    music.tracks = lambda: {"i can be more": "C:/music/I can be more.mp3", "believer": "C:/music/Believer.mp3"}
    a.brain = Brain(a, a.skills)
    return a


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """data/settings.json in a temporary folder; live skill configs start afresh."""
    monkeypatch.setattr(config, "USER_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(skillpack, "CONFIGS", {})
    return tmp_path / "settings.json"


def write_skill(root, sid, manifest, commands="", code=None, prompt=None):
    folder = root / sid
    folder.mkdir(parents=True)
    (folder / "skill.toml").write_text(textwrap.dedent(manifest), encoding="utf-8")
    if commands:
        (folder / "commands.toml").write_text(textwrap.dedent(commands), encoding="utf-8")
    if code is not None:
        (folder / "main.py").write_text(textwrap.dedent(code), encoding="utf-8")
    if prompt is not None:
        (folder / "prompts").mkdir()
        (folder / "prompts" / "hello.md").write_text(prompt, encoding="utf-8")
    return folder


# ---------------------------------------------------------------- built-in folder skills
def test_builtin_folder_skills_load(app):
    assert {"solve", "focus", "weather"} <= {m.id for m in BUILTIN}
    loaded = {s.name: s for s in app.skills}
    for m in BUILTIN:
        assert m.id in loaded, f"{m.id} не загрузился"
        assert loaded[m.id].manifest.path == m.path
        assert not m.examples or loaded[m.id].examples == m.examples   # scenarios: examples from the code


@pytest.mark.parametrize("phrase,expected", [(p, a) for m in BUILTIN for p, a in m.test_phrases()])
def test_skill_phrases(app, phrase, expected):
    """Every skill's tests/phrases.toml, checked with all skills in their real order (a stranger skill cannot steal
    these phrases unnoticed)."""
    hit = app.brain.match_skill(phrase)
    got = f"{hit[0].name}.{hit[1].action}" if hit else "none.none"
    assert got == expected, phrase


def test_focus_settings_come_from_its_manifest(app):
    focus = next(s for s in app.skills if s.name == "focus")
    assert isinstance(focus.ctx.config.work_min, int) and isinstance(focus.ctx.config.rounds, int)
    fields = {f.key: f for f in skillpack.settings_fields(BUILTIN)}
    assert {"focus.work_min", "focus.break_min", "focus.rounds", "solve.local_fallback"} <= set(fields)


def test_settings_page_shows_and_saves_skill_settings(settings_file):
    from assistant.settings_schema import schema

    modules = next(s for s in schema([]) if s["title"] == "Модули")
    assert "focus.work_min" in {f["key"] for f in modules["fields"]}

    ours, rest = skillpack.split_changes({"focus.work_min": "50", "tts.volume": 0.8})
    assert rest == {"tts.volume": 0.8} and [c[1] for c in ours] == ["focus.work_min"]
    assert skillpack.save_changes(ours) == []
    focus = next(m for m in BUILTIN if m.id == "focus")
    assert skillpack.config_for(focus).work_min == 50                         # live, as int
    assert config.load_user_overrides() == {"focus": {"work_min": 50}}       # the same key as before the move

    with pytest.raises(ValueError):
        skillpack.split_changes({"focus.work_min": "много"})


def test_old_user_values_still_apply(settings_file):
    """Values saved when the settings lived in config.py ("focus.work_min") reach the skill."""
    settings_file.write_text('{"focus": {"work_min": 40, "rounds": "3", "unknown": 1}}', encoding="utf-8")
    focus = next(m for m in BUILTIN if m.id == "focus")
    cfg = skillpack.config_for(focus)
    assert (cfg.work_min, cfg.rounds, cfg.break_min) == (40, 3, 5)
    assert not hasattr(cfg, "unknown")


# ---------------------------------------------------------------- order
def test_order_follows_before_and_after():
    m = {sid: skillpack.Manifest(sid, sid, None, False, before=b, after=a)  # type: ignore[arg-type]
         for sid, b, a in [("x", ["calc"], []), ("y", [], ["calc"]), ("z", [], [])]}
    assert skillpack.order(["calc", "apps"], m, set()) == ["x", "calc", "y", "apps", "z"]
    assert skillpack.order(["calc", "apps"], m, {"y", "apps"}) == ["x", "calc", "z"]


# ---------------------------------------------------------------- user skills
HELLO = """
    [skill]
    id = "hello"
    name = "Привет"
    api = 1
    before = ["calc"]

    [config]
    greeting = "Здравствуйте"

    [[settings]]
    key = "greeting"
    label = "Приветствие"
    type = "text"
"""
HELLO_COMMANDS = """
    examples = ["поздоровайся"]

    [[command]]
    action = "hi"
    patterns = ['^поздоровайся']

    [[command]]
    action = "bye"
    patterns = ['^попрощайся']
    reply = "До свидания."
"""
HELLO_CODE = """
    from assistant.skills.base import Reply, Skill


    class Hello(Skill):
        async def handle(self, intent):
            if intent.action == "hi":
                return Reply(self.ctx.config.greeting + ". " + self.ctx.prompt("hello"))
            return await super().handle(intent)


    def create():
        return Hello()
"""


def test_user_skill_from_folder(tmp_path, settings_file):
    write_skill(tmp_path / "skills", "hello", HELLO, HELLO_COMMANDS, HELLO_CODE, "Я Джарвис.")
    a = FakeApp()
    others = [i for i in BUILTIN_IDS if i not in ("calc", "apps")]
    a.skills = load_skills(["calc", "apps"], disabled=others, user_dir=tmp_path / "skills")
    for s in a.skills:
        s.setup(a)
    assert [s.name for s in a.skills] == ["hello", "calc", "apps", "system"]   # system cannot be turned off
    hello = a.skills[0]
    assert not hello.manifest.builtin and type(hello).__module__ == "jarvis_skills.hello.main"

    async def say(phrase):
        return (await hello.handle(hello.match(phrase))).speech

    assert asyncio.run(say("поздоровайся")) == "Здравствуйте. Я Джарвис."
    assert asyncio.run(say("попрощайся")) == "До свидания."   # a fixed reply from commands.toml, no code
    assert hello.match("открой блокнот") is None


def test_skill_without_code(tmp_path):
    manifest = HELLO.replace('api = 1', 'api = 1\n    entry = ""')
    write_skill(tmp_path, "hello", manifest, HELLO_COMMANDS)
    skills = [s for s in load_skills([], disabled=BUILTIN_IDS, user_dir=tmp_path) if s.name != "system"]
    assert [s.name for s in skills] == ["hello"]
    assert asyncio.run(skills[0].handle(skills[0].match("попрощайся"))).speech == "До свидания."


@pytest.mark.parametrize("manifest,error", [
    (HELLO.replace('id = "hello"', 'id = "other"'), "совпадать с именем папки"),
    (HELLO.replace("api = 1", "api = 99"), "версия API"),
    (HELLO.replace("api = 1", 'api = 1\n    permissions = ["root"]'), "разрешения"),
    (HELLO.replace('key = "greeting"', 'key = "missing"'), "не описано"),
    (HELLO, "нет файла кода"),
])
def test_bad_manifest_is_skipped(tmp_path, manifest, error, caplog):
    write_skill(tmp_path, "hello", manifest, HELLO_COMMANDS)
    with pytest.raises(skillpack.ManifestError, match=error):
        skillpack.read_manifest(tmp_path / "hello", builtin=False)
    assert "hello" not in {m.id for m in skillpack.discover(tmp_path)}


def test_user_skill_cannot_replace_builtin(tmp_path):
    write_skill(tmp_path, "weather", HELLO.replace('"hello"', '"weather"'), HELLO_COMMANDS, HELLO_CODE)
    weather = [m for m in skillpack.discover(tmp_path) if m.id == "weather"]
    assert len(weather) == 1 and weather[0].builtin


def test_disabled_skill_is_not_loaded(tmp_path):
    write_skill(tmp_path, "hello", HELLO, HELLO_COMMANDS, HELLO_CODE)
    assert "hello" not in {s.name for s in load_skills(["calc"], disabled=["hello"], user_dir=tmp_path)}


def test_settings_api_reads_and_writes_skill_settings(settings_file):
    from fastapi.testclient import TestClient

    from assistant.tts.manager import TtsManager
    from assistant.voicelab.server import Host, create_app

    cfg = config.load_settings()
    client = TestClient(create_app(Host(cfg, TtsManager("silero:eugene"))), base_url=f"http://127.0.0.1:{cfg.voicelab.port}")
    data = client.get("/api/settings").json()
    assert data["values"]["focus"]["work_min"] == 25 and data["values"]["solve"]["local_fallback"] is False
    assert client.post("/api/settings", json={"changes": {"focus.rounds": 6}}).status_code == 200
    assert client.get("/api/settings").json()["values"]["focus"]["rounds"] == 6
    assert client.post("/api/settings", json={"changes": {"focus.rounds": "x"}}).status_code == 400


# ---------------------------------------------------------------- prompt skills (assistant/prompting.py)
class FakeLlm:
    def __init__(self, answer, provider="local"):
        self.answer, self.last_provider, self.sent = answer, provider, []

    async def complete(self, chain, messages, **kw):
        self.sent.append((chain, messages, kw))
        return self.answer

    async def stream(self, chain, messages, **kw):
        self.sent.append((chain, messages, kw))
        yield self.answer


def prompted(app, monkeypatch, name, answer, selected="", provider="local"):
    """The skill with a fake model, a fake selection; windows, clipboard and pasting are recorded."""
    skill = next(s for s in app.skills if s.name == name)
    llm, seen = FakeLlm(answer, provider), {"cards": [], "clipboard": [], "pasted": []}
    monkeypatch.setattr(app, "llm", llm, raising=False)
    monkeypatch.setattr(app.ui, "show_deck", lambda d: seen["cards"].append((d.title, d.cards[0].screen)))

    async def selection():
        return selected

    async def clip(text):
        seen["clipboard"].append(text)

    async def paste(text):
        seen["pasted"].append(text)

    monkeypatch.setattr(skill.ctx, "selection", selection)
    monkeypatch.setattr(skill.ctx, "set_clipboard", clip)
    monkeypatch.setattr(skill.ctx, "paste", paste)
    return skill, llm, seen


def say(skill, phrase):
    from assistant.nlu import normalize_command

    async def run():
        intent = skill.match(normalize_command(phrase))
        intent.raw = phrase
        reply = await skill.handle(intent)
        return reply.speech or ("".join([d async for d in reply.stream]) if reply.stream else "")
    return asyncio.run(run())


def test_translate_selection(app, monkeypatch):
    skill, llm, seen = prompted(app, monkeypatch, "selection", "Привет, мир", selected="Hello, world")
    assert say(skill, "переведи") == "Привет, мир"                       # into Russian and short: read aloud
    chain, messages, _ = llm.sent[0]
    assert chain == ["local"] and "на русский язык" in messages[0].content and messages[1].content == "Hello, world"
    assert seen["clipboard"] == ["Привет, мир"] and seen["cards"] == [("Перевод · русский", "Привет, мир")]

    skill, llm, seen = prompted(app, monkeypatch, "selection", "Hello", selected="Привет")
    assert say(skill, "переведи выделенное") == "Перевод на экране и в буфере обмена."
    assert "на английский язык" in llm.sent[0][1][0].content


def test_fix_replaces_selection_and_empty_selection(app, monkeypatch):
    skill, llm, seen = prompted(app, monkeypatch, "selection", "Привет, как дела?", selected="привет как дила")
    assert say(skill, "исправь ошибки") == "Готово." and seen["pasted"] == ["Привет, как дела?"]
    skill, llm, seen = prompted(app, monkeypatch, "selection", "-", selected="")
    assert say(skill, "перепиши вежливо") == "Сначала выделите текст, а потом скажите, что с ним сделать."
    assert llm.sent == []


def test_read_selection_without_model(app, monkeypatch):
    skill, llm, _ = prompted(app, monkeypatch, "selection", "-", selected="Текст для чтения")
    assert say(skill, "прочитай выделенное") == "Текст для чтения" and llm.sent == []


def test_ocr_nothing_found(app, monkeypatch):
    skill, llm, seen = prompted(app, monkeypatch, "screen", "НЕТ", provider="gemini")
    skill.ctx.share("screen.region", b"old")
    monkeypatch.setattr(app.ui, "select_region", lambda: _done(b"png"))
    assert say(skill, "скопируй текст с экрана") == "Текста в этой области не нашёл."
    assert llm.sent[0][0] == ["gemini", "local"] and llm.sent[0][1][0].images == [b"png"]
    assert seen["clipboard"] == []


def _done(value):
    import concurrent.futures

    fut = concurrent.futures.Future()
    fut.set_result(value)
    return fut


PROMPT_SKILL = """
    [skill]
    id = "joke"
    name = "Шутка про"
    api = 1
    entry = ""
"""
PROMPT_COMMANDS = """
    examples = ["пошути про <тема>"]

    [[command]]
    action = "joke"
    patterns = ['^пошути про ']
    source = "said"
    prompt = "joke"
    chain = "info"
    output = "card"
    title = "Шутка"
    speak_max = 300
"""


def test_prompt_skill_without_code(tmp_path, settings_file):
    folder = write_skill(tmp_path, "joke", PROMPT_SKILL, PROMPT_COMMANDS)
    (folder / "prompts").mkdir()
    (folder / "prompts" / "joke.md").write_text("Короткая шутка по просьбе: «{phrase}».", encoding="utf-8")
    a = FakeApp()
    a.skills = load_skills([], disabled=BUILTIN_IDS, user_dir=tmp_path)
    for s in a.skills:
        s.setup(a)
    skill = next(s for s in a.skills if s.name == "joke")
    llm, cards = FakeLlm("Программист заходит в бар…", "groq"), []
    a.llm = llm
    a.ui.show_deck = lambda d: cards.append(d.title)
    assert say(skill, "Пошути про котов") == "Программист заходит в бар…"
    chain, messages, _ = llm.sent[0]
    assert chain == a.cfg.llm.info_chain and messages[0].content == "Короткая шутка по просьбе: «Пошути про котов»."
    assert messages[1].content == "Пошути про котов" and cards == ["Шутка"]


@pytest.mark.parametrize("old,new,error", [
    ("", "", "нет файла prompts/joke.md"),
    ('output = "card"', 'output = "poster"', "output"),
    ('output = "card"', 'colour = "red"', "неизвестное поле"),
    ('chain = "info"', 'chain = "@missing"', "не описано в"),
])
def test_bad_prompt_command(tmp_path, old, new, error):
    folder = write_skill(tmp_path, "joke", PROMPT_SKILL, PROMPT_COMMANDS.replace(old, new) if old else PROMPT_COMMANDS)
    if old:
        (folder / "prompts").mkdir()
        (folder / "prompts" / "joke.md").write_text("x", encoding="utf-8")
    with pytest.raises(skillpack.ManifestError, match=error):
        skillpack.read_manifest(folder, builtin=False)


# ---------------------------------------------------------------- priorities, turning off, reading again
def test_builtin_order_comes_from_priorities():
    from assistant.skills.base import plan_skills

    order = [n for n, _ in plan_skills([], [])]
    assert order[:3] == ["scenarios", "solve", "calc"] and order[-1] == "apps"
    assert "system" in [n for n, _ in plan_skills([], ["system", "news"])]          # required: stays
    assert "news" not in [n for n, _ in plan_skills([], ["system", "news"])]


def _reloading_app(tmp_path, monkeypatch):
    import types

    from assistant.assistant import Assistant

    monkeypatch.setattr(skillpack, "USER_DIR", tmp_path)
    a = FakeApp()
    a.skills = load_skills([], disabled=BUILTIN_IDS)
    a.brain = Brain(a, a.skills)
    a._stop_skill = types.MethodType(Assistant._stop_skill, a)
    a.reload = lambda: [n for n in asyncio.run(Assistant.reload_skills(a)) if n != "system"]
    return a


def test_reload_picks_up_new_edited_and_removed_skills(tmp_path, monkeypatch, settings_file):
    a = _reloading_app(tmp_path, monkeypatch)
    monkeypatch.setattr(a.cfg.skills, "disabled", BUILTIN_IDS)
    assert a.reload() == []

    def hello():
        return next(s for s in a.skills if s.name == "hello")

    folder = write_skill(tmp_path, "hello", HELLO, HELLO_COMMANDS, HELLO_CODE, "Я Джарвис.")
    assert a.reload() == ["hello"]
    first = hello()
    assert a.brain.match_skill("поздоровайся")[0] is first
    assert a.reload() == ["hello"] and hello() is first                       # unchanged: the same object

    import os
    import time
    (folder / "prompts" / "hello.md").write_text("Я снова тут.", encoding="utf-8")
    later = time.time() + 5
    os.utime(folder / "prompts" / "hello.md", (later, later))
    a.reload()
    assert hello() is not first
    assert asyncio.run(hello().handle(hello().match("поздоровайся"))).speech == "Здравствуйте. Я снова тут."

    (folder / "main.py").write_text("this is not python", encoding="utf-8")
    os.utime(folder / "main.py", (later + 5, later + 5))
    working = hello()
    a.reload()
    assert hello() is working                                                  # broken edit: the old one stays

    skillpack.remove_user_skill("hello", tmp_path)
    assert a.reload() == [] and a.brain.match_skill("поздоровайся") is None
    assert list((tmp_path.parent / "skills_removed").iterdir())


def test_skills_api(tmp_path, monkeypatch, settings_file):
    from fastapi.testclient import TestClient

    from assistant.tts.manager import TtsManager
    from assistant.voicelab.server import Host, create_app

    user = tmp_path / "skills"
    monkeypatch.setattr(skillpack, "USER_DIR", user)
    write_skill(user, "hello", HELLO, HELLO_COMMANDS, HELLO_CODE)
    write_skill(user, "broken", HELLO.replace('"hello"', '"other"'), HELLO_COMMANDS, HELLO_CODE)
    cfg = config.load_settings()
    client = TestClient(create_app(Host(cfg, TtsManager("silero:eugene"))), base_url=f"http://127.0.0.1:{cfg.voicelab.port}")
    data = client.get("/api/skills").json()
    skills = {s["id"]: s for s in data["skills"]}
    assert skills["hello"]["builtin"] is False and skills["focus"]["builtin"] is True and skills["system"]["required"]
    assert [e["folder"] for e in data["errors"]] == ["broken"]

    assert client.post("/api/skills/toggle", json={"id": "news", "enabled": False}).json() == {"ok": True, "live": False}
    assert config.load_user_overrides()["skills"]["disabled"] == ["news"]
    assert client.get("/api/skills").json()["skills"][[s["id"] for s in client.get("/api/skills").json()["skills"]]
                                                       .index("news")]["enabled"] is False
    assert client.post("/api/skills/toggle", json={"id": "system", "enabled": False}).status_code == 400
    assert client.post("/api/skills/remove", json={"id": "focus"}).status_code == 400       # built-in: not removable
    assert client.post("/api/skills/remove", json={"id": "hello"}).status_code == 200
    assert not (user / "hello").exists()
