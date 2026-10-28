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
        assert loaded[m.id].manifest.path == m.path and loaded[m.id].examples == m.examples


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
    a.skills = load_skills(["calc", "apps"], disabled=BUILTIN_IDS, user_dir=tmp_path / "skills")
    for s in a.skills:
        s.setup(a)
    assert [s.name for s in a.skills] == ["hello", "calc", "apps"]
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
    skills = load_skills([], disabled=BUILTIN_IDS, user_dir=tmp_path)
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
