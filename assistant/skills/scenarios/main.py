"""User scenarios: a phrase -> a sequence of steps. Kept in data/scenarios.toml, edited with buttons in
Settings -> Сценарии (tray -> "Мои сценарии…"), which writes the file through dump(); the file is re-read on change.

Steps: open, url, close, say, reaction, wait, timer, media, music, search, minimize_all, command.
`command` runs any other Jarvis command, e.g. { command = "доброе утро" }.
"""
from __future__ import annotations

import asyncio
import json
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


HEADER = """# Мои сценарии Джарвиса: фраза -> действия по порядку.
# Удобнее всего менять их в Настройках -> Сценарии (трей -> «Мои сценарии…»): там всё кнопками.
# Файл перечитывается сам, перезапуск не нужен.
#
# Сценарий:
#   name    = "Название"                 для вас; вслух не произносится
#   phrases = ["фраза", "ещё фраза"]     что сказать после «Джарвис»
#   say     = "Ответ"                    что он ответит (можно не писать: тогда «Слушаюсь, сэр»)
#   steps   = [ { шаг }, { шаг } ]       что сделать, по порядку
#
# Шаги:
#   { open = "телеграм" }                    программа по названию, путь к .exe / .lnk или ссылка steam://…
#   { url = "https://…" }                    сайт в браузере
#   { close = "браузер" }                    закрыть программу
#   { say = "текст" }                        сказать фразу
#   { reaction = "ok" }                      реплика Джарвиса: ok, reply, thanks, joke, game_mode
#   { wait = 2 }                             пауза, секунды
#   { timer = 25, label = "перерыв" }        таймер на N минут
#   { media = "play_pause" }                 play_pause, next, prev, mute, unmute, vol_up, vol_down (+ times = 5)
#   { music = "название трека" }             музыка из папки ("" = вперемешку)
#   { search = "запрос" }                    поиск в браузере
#   { minimize_all = true }                  свернуть все окна
#   { command = "доброе утро" }              любая другая команда Джарвиса
"""
STEP_KEYS = ("open", "url", "close", "say", "reaction", "wait", "timer", "media", "music", "search", "minimize_all",
             "command")


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    return json.dumps(str(v), ensure_ascii=False)   # a TOML basic string has the same escapes


def _wrapped(key: str, values: list[str], width: int = 110) -> str:
    """`key = [a, b, …]` on one line, or wrapped under the bracket when it is long."""
    line = f"{key} = [" + ", ".join(values) + "]"
    if len(line) <= width:
        return line
    lines, cur = [], ""
    for v in values:
        if cur and len(cur) + len(v) + 6 > width:
            lines.append(cur)
            cur = ""
        cur += (", " if cur else "") + v
    lines.append(cur)
    return f"{key} = [\n" + ",\n".join("    " + x for x in lines) + ",\n]"


def dump(items: list[dict]) -> str:
    """Scenarios as a tidy file: the header, then each scenario with one step per line."""
    out = [HEADER]
    for sc in items:
        out.append("[[scenario]]")
        out.append(f"name = {_toml_value(sc.get('name') or 'Сценарий')}")
        out.append(_wrapped("phrases", [_toml_value(p) for p in sc.get("phrases", [])]))
        if sc.get("say"):
            out.append(f"say = {_toml_value(sc['say'])}")
        steps = [st for st in sc.get("steps", []) if isinstance(st, dict) and st]
        if steps:
            out.append("steps = [")
            out += ["    { " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in st.items()) + " }," for st in steps]
            out.append("]")
        else:
            out.append("steps = []")
        out.append("")
    return "\n".join(out)


def load(text: str) -> list[dict]:
    """The scenarios of a file as plain data (for the editor)."""
    data = tomllib.loads(text)
    return [{"name": str(s.get("name", "")), "phrases": [str(p) for p in s.get("phrases", [])],
             "say": str(s.get("say", "")), "steps": [dict(st) for st in s.get("steps", []) if isinstance(st, dict)]}
            for s in data.get("scenario", [])]


def check(items: list[dict]) -> list[dict]:
    """Cleans what the editor sent; raises ValueError with a message for the user."""
    clean, seen = [], {}
    for i, sc in enumerate(items, 1):
        name = str(sc.get("name", "")).strip() or f"Сценарий {i}"
        phrases = list(dict.fromkeys(p.strip() for p in sc.get("phrases", []) if str(p).strip()))
        if not phrases:
            raise ValueError(f"У сценария «{name}» нет ни одной фразы")
        for p in phrases:
            key = normalize_command(p)
            if key in seen and seen[key] != name:
                raise ValueError(f"Фраза «{p}» есть и в «{seen[key]}», и в «{name}»")
            seen[key] = name
        steps = []
        for st in sc.get("steps", []):
            kind = next((k for k in STEP_KEYS if k in st), None)
            if kind is None:
                continue
            value = st[kind]
            if kind in ("wait", "timer"):
                try:
                    value = float(str(value).replace(",", "."))
                except ValueError:
                    raise ValueError(f"«{name}»: в шаге {kind} нужно число, а не «{value}»") from None
            elif kind == "minimize_all":
                value = True
            elif kind != "music" and not str(value).strip():
                continue   # an empty step the user added and did not fill
            steps.append({kind: value, **{k: v for k, v in st.items() if k not in STEP_KEYS and v not in ("", None)}})
        clean.append({"name": name, "phrases": phrases, "say": str(sc.get("say", "")).strip(), "steps": steps})
    return clean


@dataclass
class Scenario:
    name: str
    phrases: list[str]
    steps: list[dict] = field(default_factory=list)
    say: str = ""


class ScenariosSkill(Skill):
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

    def examples(self) -> list[str]:  # type: ignore[override]
        return [f"{sc.phrases[0]} (сценарий «{sc.name}»)" for sc in self.scenarios() if sc.phrases]

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
                from assistant.skills.apps.main import _launch

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
