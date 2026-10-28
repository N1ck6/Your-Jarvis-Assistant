"""Skills as folders: a manifest, phrases, prompts and code next to each other. SKILLS.md describes the format.

Built-in skills live in assistant/skills/<id>/, the user's own (or installed) ones in data/skills/<id>/: updates of
Jarvis never touch the latter. A folder skill is found by its skill.toml; the old single-file modules
(assistant/skills/<name>.py) still load as before, so skills move to folders one by one.
"""
from __future__ import annotations

import copy
import logging
import re
import sys
import tomllib
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from assistant.paths import DATA_DIR

log = logging.getLogger("skills")

API_VERSION = 1
BUILTIN_DIR = Path(__file__).resolve().parent / "skills"
USER_DIR = DATA_DIR / "skills"
USER_PACKAGE = "jarvis_skills"   # data/skills/<id>/main.py is imported as jarvis_skills.<id>.main
PERMISSIONS = {"screen", "clipboard", "cloud_llm", "local_llm", "browser", "files", "power", "keyboard", "network",
               "microphone"}
_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class ManifestError(ValueError):
    pass


@dataclass
class Command:
    """A trigger from commands.toml: the first pattern that matches the normalized phrase gives the action."""
    action: str
    patterns: list[re.Pattern[str]]
    reply: str = ""   # a fixed answer: such a command needs no code at all


@dataclass
class Manifest:
    id: str
    name: str
    path: Path
    builtin: bool
    version: str = "0.1.0"
    author: str = ""
    description: str = ""
    api: int = API_VERSION
    entry: str = "main:create"       # module:function inside the folder; "" for a skill made of commands only
    before: list[str] = field(default_factory=list)   # match phrases before these skills
    after: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)  # other skills it works with (a warning if one is missing)
    permissions: list[str] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)          # defaults; the user's values: data/settings.json[id]
    settings: list[dict[str, Any]] = field(default_factory=list)  # fields for the settings page
    examples: list[str] = field(default_factory=list)             # canonical phrases for the router catalog
    commands: list[Command] = field(default_factory=list)

    def prompt(self, name: str) -> str:
        """prompts/<name>.md of the skill."""
        return (self.path / "prompts" / f"{name}.md").read_text(encoding="utf-8").strip()

    def test_phrases(self) -> list[tuple[str, str]]:
        """tests/phrases.toml: [[phrase]] text = "...", action = "..." — what each phrase must trigger."""
        file = self.path / "tests" / "phrases.toml"
        if not file.exists():
            return []
        data = tomllib.loads(file.read_text(encoding="utf-8"))
        return [(str(p["text"]), str(p.get("skill", self.id)) + "." + str(p["action"])) for p in data.get("phrase", [])]


def read_manifest(folder: Path, builtin: bool) -> Manifest:
    try:
        data = tomllib.loads((folder / "skill.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"{folder.name}/skill.toml: {exc}") from exc
    skill = data.get("skill", {})
    sid = str(skill.get("id", ""))
    if sid != folder.name or not _ID.match(sid):
        raise ManifestError(f"{folder.name}: id «{sid}» должен совпадать с именем папки (латиница, цифры, _)")
    api = int(skill.get("api", API_VERSION))
    if api > API_VERSION:
        raise ManifestError(f"{sid}: нужна версия API {api}, у Джарвиса {API_VERSION} — обновите Джарвиса")
    unknown = set(skill.get("permissions", [])) - PERMISSIONS
    if unknown:
        raise ManifestError(f"{sid}: неизвестные разрешения {sorted(unknown)}")
    m = Manifest(id=sid, name=str(skill.get("name", sid)), path=folder, builtin=builtin,
                 version=str(skill.get("version", "0.1.0")), author=str(skill.get("author", "")),
                 description=str(skill.get("description", "")), api=api, entry=str(skill.get("entry", "main:create")),
                 before=list(skill.get("before", [])), after=list(skill.get("after", [])),
                 requires=list(skill.get("requires", [])), permissions=list(skill.get("permissions", [])),
                 config=dict(data.get("config", {})), settings=list(data.get("settings", [])))
    for s in m.settings:
        if s.get("key") not in m.config:
            raise ManifestError(f"{sid}: поле настроек «{s.get('key')}» не описано в [config]")
    commands_file = folder / "commands.toml"
    if commands_file.exists():
        try:
            cmds = tomllib.loads(commands_file.read_text(encoding="utf-8"))
            m.examples = [str(e) for e in cmds.get("examples", [])]
            m.commands = [Command(str(c["action"]), [re.compile(p) for p in c.get("patterns", [])], str(c.get("reply", "")))
                          for c in cmds.get("command", [])]
        except (tomllib.TOMLDecodeError, KeyError, re.error) as exc:
            raise ManifestError(f"{sid}/commands.toml: {exc}") from exc
    if m.entry and not (folder / (m.entry.split(":")[0].replace(".", "/") + ".py")).exists():
        raise ManifestError(f"{sid}: нет файла кода для entry = «{m.entry}»")
    if not m.entry and not any(c.reply for c in m.commands):
        raise ManifestError(f"{sid}: навык без кода должен отвечать хотя бы на одну команду (reply)")
    return m


def discover(user_dir: Path | None = None) -> list[Manifest]:
    """Built-in folder skills, then the user's. A user skill cannot replace a built-in one with the same id."""
    found: dict[str, Manifest] = {}
    for root, builtin in ((BUILTIN_DIR, True), (user_dir or USER_DIR, False)):
        if not root.is_dir():
            continue
        for folder in sorted(p for p in root.iterdir() if (p / "skill.toml").is_file()):
            try:
                m = read_manifest(folder, builtin)
            except ManifestError as exc:
                log.error("Навык не загружен: %s", exc)
                continue
            if m.id in found:
                log.error("Навык %s из %s пропущен: такой уже есть", m.id, folder)
                continue
            found[m.id] = m
    return list(found.values())


def order(names: list[str], manifests: dict[str, Manifest], disabled: set[str]) -> list[str]:
    """[skills] enabled keeps its order; folder skills that are not in it go before/after the skills they name,
    otherwise to the end."""
    out = [n for n in names if n not in disabled]
    for m in manifests.values():
        if m.id in out or m.id in disabled:
            continue
        before = [out.index(b) for b in m.before if b in out]
        after = [out.index(a) for a in m.after if a in out]
        if before:
            out.insert(min(before), m.id)
        elif after:
            out.insert(max(after) + 1, m.id)
        else:
            out.append(m.id)
    return out


def import_entry(m: Manifest):
    """The skill's create() function. Built-in: assistant.skills.<id>.<module>; user: jarvis_skills.<id>.<module>."""
    import importlib

    module_name, _, func = m.entry.partition(":")
    if m.builtin:
        package = f"assistant.skills.{m.id}"
    else:
        root = sys.modules.get(USER_PACKAGE)
        if root is None:
            root = types.ModuleType(USER_PACKAGE)
            root.__path__ = []  # type: ignore[attr-defined]
            sys.modules[USER_PACKAGE] = root
        if str(m.path.parent) not in root.__path__:  # type: ignore[attr-defined]
            root.__path__.append(str(m.path.parent))  # type: ignore[attr-defined]
        package = f"{USER_PACKAGE}.{m.id}"
    module = importlib.import_module(f"{package}.{module_name}")
    return getattr(module, func or "create")


# ---------------------------------------------------------------- per-skill settings
class SkillConfig:
    """The skill's settings: [config] defaults from skill.toml with the user's values from data/settings.json[id].
    Read as attributes: ctx.config.work_min. The settings page changes the same object, so the skill sees new values
    at once."""

    def __init__(self, defaults: dict[str, Any], values: dict[str, Any] | None = None) -> None:
        object.__setattr__(self, "_defaults", copy.deepcopy(defaults))
        data = copy.deepcopy(defaults)
        for key, value in (values or {}).items():
            if key in defaults:
                try:
                    data[key] = coerce(defaults[key], value)
                except (TypeError, ValueError):
                    log.warning("Настройка %s = %r не подходит, беру значение по умолчанию", key, value)
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self.set(name, value)

    def set(self, key: str, value: Any) -> None:
        if key not in self._defaults:
            raise KeyError(f"нет такой настройки: {key}")
        self._data[key] = coerce(self._defaults[key], value)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)


def coerce(default: Any, value: Any) -> Any:
    """The settings page and data/settings.json may give "25" or "false": the type of the default wins."""
    if isinstance(default, bool):
        if isinstance(value, str):
            if value.strip().lower() in ("true", "1", "да", "yes", "on"):
                return True
            if value.strip().lower() in ("false", "0", "нет", "no", "off", ""):
                return False
            raise ValueError(value)
        return bool(value)
    if isinstance(default, int):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    if isinstance(default, list):
        if not isinstance(value, list):
            raise ValueError(value)
        return list(value)
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise ValueError(value)
        return dict(value)
    return str(value)


# One live config per skill id: the skill and the settings page share it.
CONFIGS: dict[str, SkillConfig] = {}


def config_for(m: Manifest) -> SkillConfig:
    from assistant.config import load_user_overrides

    if m.id not in CONFIGS:
        CONFIGS[m.id] = SkillConfig(m.config, load_user_overrides().get(m.id) or {})
    return CONFIGS[m.id]


def settings_fields(manifests: list[Manifest]):
    """Fields of all folder skills for the settings page ("Модули"): key "<id>.<name>"."""
    from assistant.settings_schema import F

    out = []
    for m in manifests:
        for s in m.settings:
            options = [list(o) for o in s.get("options", [])]
            out.append(F(f"{m.id}.{s['key']}", str(s.get("label", s["key"])), str(s.get("type", "text")),
                         str(s.get("hint", "")), s.get("min"), s.get("max"), s.get("step"), options,
                         bool(s.get("restart", False))))
    return out


def settings_values(manifests: list[Manifest]) -> dict[str, dict[str, Any]]:
    return {m.id: config_for(m).as_dict() for m in manifests if m.config}


SkillChange = tuple[Manifest, str, str, Any]   # (skill, "focus.work_min", "work_min", checked value)


def split_changes(changes: dict[str, Any],
                  manifests: list[Manifest] | None = None) -> tuple[list[SkillChange], dict[str, Any]]:
    """Changes from the settings page -> (those of folder skills, checked; the rest, for the core settings).
    Raises ValueError on a wrong value of a skill setting. Nothing is saved yet: see save_changes."""
    by_id = {m.id: m for m in (manifests if manifests is not None else discover())}
    ours: list[SkillChange] = []
    rest: dict[str, Any] = {}
    for dotted, value in changes.items():
        sid, _, key = dotted.partition(".")
        m = by_id.get(sid)
        if m is None or key not in m.config:
            rest[dotted] = value
            continue
        try:
            ours.append((m, dotted, key, coerce(m.config[key], value)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{dotted}: неверное значение {value!r}") from exc
    return ours, rest


def save_changes(ours: list[SkillChange]) -> list[str]:
    """Saves checked skill settings and applies them live; returns the keys that need a restart."""
    from assistant.config import save_override

    restart = []
    for m, dotted, key, value in ours:
        config_for(m).set(key, value)
        save_override(dotted, value)
        if any(s.get("key") == key and s.get("restart") for s in m.settings):
            restart.append(dotted)
    return restart
