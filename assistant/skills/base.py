"""Skill (module) framework.

A skill is a self-contained feature: it can be triggered by a fast regex match
(no LLM, milliseconds) and/or exposed to the local LLM as a tool.

To add a skill: create assistant/skills/<name>.py with `def create() -> Skill`
and add <name> to [skills].enabled in config. See docs/MODULES.md.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from abc import ABC
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

if TYPE_CHECKING:
    from assistant.assistant import Assistant

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

    def setup(self, app: "Assistant") -> None:
        self.app = app

    def match(self, text: str) -> Intent | None:
        """Fast path. `text` is normalized: lowercase, no punctuation, "ё" -> "е", wake word removed."""
        return None

    def tools(self) -> list[Tool]:
        return []

    async def handle(self, intent: Intent) -> Reply:
        raise NotImplementedError

    async def start(self) -> None:
        """Called once the event loop runs (restore timers, warm caches...)."""

    async def stop(self) -> None:
        pass


def load_skills(names: list[str]) -> list[Skill]:
    skills: list[Skill] = []
    for name in names:
        try:
            module = importlib.import_module(f"assistant.skills.{name}")
            skills.append(module.create())
        except Exception:
            log.exception("Модуль %s не загружен", name)
    log.info("Модули: %s", ", ".join(s.name for s in skills))
    return skills


async def run_blocking(func, *args):
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)
