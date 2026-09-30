"""User scenarios: a phrase -> a sequence of steps. Defined in data/scenarios.toml (tray -> "Мои сценарии").

Steps: open, url, close, say, reaction, wait, timer, media, music, search, minimize_all, command.
`command` runs any other Jarvis command, e.g. { command = "доброе утро" }. The file is re-read on change.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tomllib
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

from assistant import winutil
from assistant.nlu import normalize_command
from assistant.paths import CONFIG_DIR, DATA_DIR
from assistant.skills.base import Intent, Reply, Skill, run_blocking

log = logging.getLogger("scenarios")
EXAMPLE = CONFIG_DIR / "scenarios.example.toml"


def scenarios_file() -> Path:
    path = DATA_DIR / "scenarios.toml"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(EXAMPLE, path)
    return path


@dataclass
class Scenario:
    name: str
    phrases: list[str]
    steps: list[dict] = field(default_factory=list)
    say: str = ""


class ScenariosSkill(Skill):
    name = "scenarios"
    title = "Мои сценарии"

    def __init__(self) -> None:
        self._items: list[Scenario] = []
        self._stamp = -1.0

    def scenarios(self) -> list[Scenario]:
        path = scenarios_file()
        stamp = path.stat().st_mtime
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                self._items = [Scenario(s.get("name", "сценарий"), [normalize_command(p) for p in s.get("phrases", [])],
                                        list(s.get("steps", [])), s.get("say", ""))
                               for s in data.get("scenario", [])]
                log.info("Сценариев: %d", len(self._items))
            except (tomllib.TOMLDecodeError, OSError) as exc:
                log.error("Ошибка в %s: %s", path, exc)
                self.app.ui.notify("Сценарии", f"Ошибка в файле сценариев: {exc}")
        return self._items

    def match(self, text: str) -> Intent | None:
        for sc in self.scenarios():
            for phrase in sc.phrases:
                if text == phrase or (len(phrase.split()) >= 2 and fuzz.ratio(text, phrase) >= 90):
                    return Intent(self.name, "run", {"name": sc.name}, text)
        return None

    async def handle(self, intent: Intent) -> Reply:
        sc = next((s for s in self.scenarios() if s.name == intent.slots["name"]), None)
        if sc is None:
            return Reply("Сценарий не найден.")
        log.info("Сценарий «%s»: %d шагов", sc.name, len(sc.steps))
        app = self.app
        if sc.say:
            asyncio.create_task(app.speaker.say(sc.say))
        elif app.cfg.voice.ok_for_actions:
            asyncio.create_task(app.react("ok"))
        for step in sc.steps:
            try:
                await self._step(step)
            except Exception:
                log.exception("Шаг %s сценария «%s»", step, sc.name)
        return Reply(spoken=True, listen_after=False)

    def _skill(self, name: str):
        return next((s for s in self.app.skills if s.name == name), None)

    async def _step(self, step: dict) -> None:
        app = self.app
        if "open" in step:
            target = str(step["open"])
            if "://" in target or target.endswith((".exe", ".lnk", ".url")) or ":\\" in target or target.startswith("ms-"):
                from assistant.skills.apps import _launch

                await run_blocking(_launch, target)
            else:
                await self._skill("apps").handle(Intent("apps", "open", {"name": target}))
        if "url" in step:
            await run_blocking(webbrowser.open, str(step["url"]))
        if "close" in step:
            await self._skill("windows").handle(Intent("windows", "close", {"name": str(step["close"])}))
        if "say" in step:
            await app.speaker.say(str(step["say"]))
        if "reaction" in step:
            await app.react(str(step["reaction"]))
        if "wait" in step:
            await asyncio.sleep(float(step["wait"]))
        if "timer" in step:
            await self._skill("timers").handle(Intent("timers", "set", {"minutes": float(step["timer"]),
                                                                         "label": step.get("label", "")}))
        if "media" in step:
            action = str(step["media"])
            if action in ("mute", "unmute"):
                await run_blocking(winutil.set_mute, action == "mute")
            elif action in winutil.MEDIA_KEYS:
                await run_blocking(winutil.press, winutil.MEDIA_KEYS[action], int(step.get("times", 1)))
        if "music" in step:
            await self._skill("music").handle(Intent("music", "play" if step["music"] else "shuffle", {"name": step["music"]}))
        if "search" in step:
            await self._skill("search").handle(Intent("search", "search", {"q": step["search"],
                                                                            "engine": step.get("engine", "")}))
        if step.get("minimize_all"):
            await run_blocking(winutil.minimize_all)
        if "command" in step:
            hit = app.brain.match_skill(str(step["command"]))
            if hit and hit[0] is not self:
                reply = await hit[0].handle(hit[1])
                if reply.speech and not reply.spoken:
                    await app.speaker.say(reply.speech)


def create() -> Skill:
    return ScenariosSkill()
