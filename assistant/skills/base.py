"""Skill (module) framework.

A skill is a self-contained feature: it can be triggered by a fast regex match
(no LLM, milliseconds) and/or exposed to the local LLM as a tool.

To add a skill: a folder data/skills/<id>/ with skill.toml, commands.toml and main.py (`def create() -> Skill`);
the skill uses Jarvis through `self.ctx` (Context below). See SKILLS.md.
Old single-file modules (assistant/skills/<name>.py, listed in [skills].enabled) still load.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import time
from abc import ABC
from pathlib import Path
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

if TYPE_CHECKING:
    from assistant.assistant import Assistant
    from assistant.skillpack import Manifest

log = logging.getLogger("skills")


@dataclass
class Intent:
    skill: str
    action: str
    slots: dict[str, Any] = field(default_factory=dict)
    text: str = ""   # normalized command (lowercase, no punctuation, ё -> е)
    raw: str = ""    # the phrase as recognized, with case and punctuation (set by the brain)


@dataclass
class Reply:
    speech: str = ""                          # spoken as is
    stream: AsyncIterator[str] | None = None  # or spoken while it streams
    tool_result: str = ""                     # what the LLM sees when called as a tool (defaults to speech)
    spoken: bool = False                      # the skill already spoke by itself (e.g. explain deck)
    listen_after: bool = True                 # open the hot window afterwards
    # Recorded Jarvis reaction ("ok", "thanks", "joke"...). If the voice pack has it, it replaces `speech`.
    reaction: str = ""
    # Dangerous action: `speech` asks the question, `confirm` runs after the user says "да".
    confirm: Callable[[], Awaitable["Reply"]] | None = None
    # Runs when the answer to `confirm` is "нет", another phrase or nothing at all (the browser agent waits for it).
    deny: Callable[[], Awaitable["Reply"]] | None = None
    # The phrase looked like this skill's command but it cannot serve it ("открой что-нибудь послушать"):
    # the brain asks the router first and speaks this reply only if the router has nothing better.
    fallthrough: bool = False
    # How to take the action back ("верни как было", "не то"): returns the reply to speak.
    undo: Callable[[], Awaitable["Reply"]] | None = None
    # A clarifying question in `speech` ("Закрыть Chrome или Яндекс Браузер?"): the next phrase goes here first;
    # the handler returns None if the phrase is not an answer (it is then handled as a new request).
    ask: Callable[[str], Awaitable["Reply | None"]] | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    action: str
    # direct=True: the reply goes straight to the user, the LLM does not rephrase it.
    direct: bool = True
    # exclusive=True: the skill drives voice/UI itself, so it runs after the current speech ends.
    exclusive: bool = False

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


class Skill(ABC):
    name: str = ""
    title: str = ""          # human name for docs/tray
    examples: list[str] = []  # canonical commands for the router catalog ("<...>" = a value)
    manifest: "Manifest | None" = None  # a folder skill (assistant/skills/<id>/, data/skills/<id>/); None: old module

    def setup(self, app: "Assistant") -> None:
        self.app = app
        self.ctx = Context(app, self)

    def match(self, text: str) -> Intent | None:
        """Fast path. `text` is normalized: lowercase, no punctuation, "ё" -> "е", wake word removed.
        A folder skill matches the patterns of its commands.toml by default."""
        for command in self.manifest.commands if self.manifest else []:
            if any(p.search(text) for p in command.patterns):
                return Intent(self.name, command.action, text=text)
        return None

    def tools(self) -> list[Tool]:
        return []

    def followup(self, text: str, last: Intent) -> Intent | None:
        """A short follow-up to this skill's previous command ("а завтра?", "а в Сочи?") -> updated intent."""
        return None

    async def handle(self, intent: Intent) -> Reply:
        """A command of commands.toml with a fixed reply needs no code."""
        command = next((c for c in self.manifest.commands if c.action == intent.action), None) if self.manifest else None
        if command is not None and command.reply:
            return Reply(command.reply)
        raise NotImplementedError

    async def start(self) -> None:
        """Called once the event loop runs (restore timers, warm caches...)."""

    async def stop(self) -> None:
        pass


class _CommandsOnly(Skill):
    """A folder skill without code: phrases and fixed replies from commands.toml."""


class Context:
    """What a skill may use: `self.ctx` (API version assistant.skillpack.API_VERSION). Skills written against it keep
    working while the internals of Jarvis change; `self.app` remains for the old modules."""

    def __init__(self, app: "Assistant", skill: Skill) -> None:
        from assistant import skillpack

        self.app = app
        self.skill = skill
        self.manifest = skill.manifest
        self.config = skillpack.config_for(skill.manifest) if skill.manifest and skill.manifest.config else None
        self.settings = app.cfg  # global settings, read only: city, address...

    # ---------------------------------------------------------------- files
    def prompt(self, name: str) -> str:
        """prompts/<name>.md of this skill."""
        if self.manifest is None:
            raise RuntimeError("промпты есть только у навыков-папок")
        return self.manifest.prompt(name)

    @property
    def data_dir(self) -> Path:
        """A folder for the skill's own files (data/skill_data/<id>), created on first use."""
        from assistant.paths import DATA_DIR

        path = DATA_DIR / "skill_data" / self.skill.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---------------------------------------------------------------- screen
    def card(self, title: str, text: str, pinned: bool = True) -> None:
        """A window with text. pinned: its own window that stays until ✕ (several stand side by side)."""
        from assistant.core import Card, Deck

        self.app.ui.show_deck(Deck(title=title, cards=[Card("", text, "")], done=True, pinned=pinned))

    def notify(self, title: str, text: str) -> None:
        self.app.ui.notify(title, text)

    async def select_region(self, timeout: float = 90) -> bytes | None:
        """The user drags a rectangle on a freeze-frame of the screen: PNG bytes, or None if cancelled.
        The region is shared as "screen.region" for the next request ("реши эту задачу")."""
        try:
            png = await asyncio.wait_for(asyncio.wrap_future(self.app.ui.select_region()), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if png:
            self.share("screen.region", png)
        return png

    # ---------------------------------------------------------------- voice
    async def say(self, text: str, cue: Any = None) -> None:
        """Speaks on its own (timers, background work): waits for the current answer to finish."""
        await self.app.announce(text, cue)

    # ---------------------------------------------------------------- models
    async def ask(self, messages: list[Any], *, chain: list[str] | None = None, web: bool = False,
                  max_tokens: int | None = None, models: dict[str, list[str]] | None = None) -> str:
        """The whole answer of the first provider in `chain` that answers (default: the local model)."""
        return await self.app.llm.complete(chain or ["local"], messages, web=web, max_tokens=max_tokens, models=models)

    def stream(self, messages: list[Any], *, chain: list[str] | None = None, web: bool = False,
               max_tokens: int | None = None, models: dict[str, list[str]] | None = None) -> AsyncIterator[str]:
        """The answer as it is generated: give it to Reply(stream=...) to speak it while it comes."""
        return self.app.llm.stream(chain or ["local"], messages, web=web, max_tokens=max_tokens, models=models)

    @property
    def last_provider(self) -> str:
        """Who answered the last request: gemini, groq, local or "кэш"."""
        return self.app.llm.last_provider

    # ---------------------------------------------------------------- text
    async def selection(self) -> str:
        """The text selected in the foreground window ("" if nothing is selected); the clipboard is restored."""
        from assistant import winutil

        return ((await run_blocking(winutil.copy_selection)) or "").strip()

    async def set_clipboard(self, text: str) -> None:
        from assistant import winutil

        await run_blocking(winutil.set_clipboard_text, text)

    # ---------------------------------------------------------------- between skills
    def share(self, key: str, value: Any) -> None:
        """Leaves a value for other skills: "screen.region" is the last selected region of the screen."""
        self.app.__dict__.setdefault("shared", {})[key] = (time.monotonic(), value)

    def recent(self, key: str, max_age_sec: float) -> Any:
        """A shared value if it is younger than max_age_sec, else None."""
        item = self.app.__dict__.get("shared", {}).get(key)
        if item and time.monotonic() - item[0] < max_age_sec:
            return item[1]
        return None

    def other(self, name: str) -> Skill | None:
        """Another loaded skill by id (name it in `requires` of skill.toml)."""
        return next((s for s in self.app.skills if s.name == name), None)


def load_skills(names: list[str], disabled: list[str] | None = None, user_dir: Path | None = None) -> list[Skill]:
    """Folder skills (assistant/skills/<id>/, data/skills/<id>/) and old modules (assistant/skills/<name>.py).
    The order of `names` is the matching order; folder skills not named there go by their before/after."""
    from assistant import skillpack

    manifests = {m.id: m for m in skillpack.discover(user_dir)}
    skills: list[Skill] = []
    for name in skillpack.order(names, manifests, set(disabled or [])):
        try:
            m = manifests.get(name)
            if m is None:
                module = importlib.import_module(f"assistant.skills.{name}")
                skills.append(module.create())
                continue
            skill = skillpack.import_entry(m)() if m.entry else _CommandsOnly()
            skill.manifest = m
            skill.name = m.id
            skill.title = skill.title or m.name
            if m.examples:
                skill.examples = m.examples
            skills.append(skill)
        except Exception:
            log.exception("Модуль %s не загружен", name)
    loaded = {s.name for s in skills}
    for s in skills:
        missing = [r for r in (s.manifest.requires if s.manifest else []) if r not in loaded]
        if missing:
            log.warning("Навыку %s нужны %s — они не загружены, часть функций не будет работать", s.name, missing)
    log.info("Модули: %s", ", ".join(s.name for s in skills))
    return skills


async def run_blocking(func, *args):
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)
