"""Windows and processes: close, minimize, maximize, switch to an app, list open windows.

Closing sends WM_CLOSE (the app can still ask to save work). Force-kill only after a spoken "да".
"""
from __future__ import annotations

import logging
import re

from rapidfuzz import fuzz

from assistant import winutil
from assistant.skills.apps import CYR2LAT
from assistant.skills.base import Intent, Reply, Skill, Tool, run_blocking

log = logging.getLogger("windows")

_THIS = r"(это|этот|эту|окно|это окно|текущее окно|активное окно|текущее|его|ее)"
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("minimize_all", re.compile(r"^(сверни|свернуть|спрячь|убери) (все|всё|все окна|всё окна|окна)$|^(покажи )?рабочий стол$|^закрой все окна$|^спрячь все$")),
    ("list", re.compile(r"^(какие|что за) (окна|программы|приложения) (открыты|запущены|работают)|^что (у меня )?(открыто|запущено)$|^список окон$")),
    ("kill", re.compile(r"^(убей|прибей|кильни|убить|снеси|грохни|останови процесс|заверши процесс|завершить процесс|принудительно закрой|закрой принудительно)\s+(процесс\s+)?(?P<name>.+)$")),
    ("close", re.compile(r"^(закрой|закрыть|закрывай|выключи|выключить|выруби|вырубай|заверши|завершить|выйди из|выйти из|вырубить)\s+(?P<name>.+)$")),
    ("minimize", re.compile(r"^(сверни|свернуть|спрячь|убери вниз)\s+(?P<name>.+)$")),
    ("maximize", re.compile(r"^(разверни|развернуть|раскрой|увеличь|растяни)\s+(?P<name>.+?)(\s+на (весь|полный) экран)?$|^(на весь экран|на полный экран|во весь экран|полноэкранный режим)$")),
    ("switch", re.compile(r"^(переключись|переключи|переключиться|перейди|перейти|вернись|вернуться|верни|покажи окно|открой окно|активируй|давай) (на|в|во|к) (?P<name>.+)$|^(покажи окно|открой окно|вернись к|переключи на) (?P<name2>.+)$")),
]
_BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "browser.exe", "opera.exe", "brave.exe", "vivaldi.exe", "yandex.exe"}
_NOT_WINDOWS = {"звук", "музыку", "микрофон", "свет", "таймер", "таймеры", "помодоро", "фокус"}


class WindowsSkill(Skill):
    name = "windows"
    title = "Окна и процессы"
    examples = [
        'закрой <приложение>',
        'сверни всё',
        'сверни окно',
        'разверни окно',
        'переключись на <приложение>',
        'какие окна открыты',
    ]

    def match(self, text: str) -> Intent | None:
        for action, pattern in _PATTERNS:
            m = pattern.match(text)
            if not m:
                continue
            name = (m.groupdict().get("name") or m.groupdict().get("name2") or "").strip()
            if name.split(" ")[0] in _NOT_WINDOWS:
                return None
            return Intent(self.name, action, {"name": name}, text)
        return None

    def tools(self) -> list[Tool]:
        return [Tool("window_control", "Закрыть, свернуть, развернуть окно приложения или переключиться на него.",
                     {"type": "object", "properties": {
                         "action": {"type": "string", "enum": ["close", "minimize", "maximize", "switch", "minimize_all"]},
                         "name": {"type": "string", "description": "Приложение; пусто — активное окно"}},
                      "required": ["action"]}, "tool")]

    # ------------------------------------------------------------------ lookup
    def _variants(self, name: str) -> list[str]:
        translit = self.app.cfg.apps.translit
        out = [name, translit.get(name, ""), name.translate(CYR2LAT)]
        for k, v in translit.items():
            if fuzz.ratio(name, k) >= 85:
                out.append(v)
        return [v for v in dict.fromkeys(out) if v]

    def find(self, name: str) -> list[winutil.Window]:
        """Windows of the app the user named. Empty name / "окно" -> the active window."""
        name = re.sub(r"^(приложение|программу|программа|окно|игру|вкладку)\s+", "", name).strip()
        if not name or re.fullmatch(_THIS, name):
            w = winutil.foreground_window()
            return [w] if w else []
        windows = winutil.list_windows()
        if name in ("браузер", "интернет"):
            return [w for w in windows if w.exe in _BROWSERS]
        best: tuple[int, str] = (0, "")
        for w in windows:
            for v in self._variants(name):
                score = max(fuzz.ratio(v, w.app.lower()), fuzz.partial_ratio(v, w.title.lower()) if len(v) >= 4 else 0,
                            fuzz.WRatio(v, w.title.lower()))
                if score > best[0]:
                    best = (score, w.exe)
        if best[0] < 80:
            return []
        return [w for w in windows if w.exe == best[1]]

    def _process_exe(self, name: str) -> str:
        """Process image name for an app without visible windows (tray apps)."""
        import psutil

        best = (0, "")
        for p in psutil.process_iter(["name"]):
            exe = (p.info["name"] or "").lower()
            for v in self._variants(name):
                s = fuzz.ratio(v, exe.removesuffix(".exe"))
                if s > best[0]:
                    best = (s, exe)
        return best[1] if best[0] >= 80 else ""

    # ------------------------------------------------------------------ actions
    async def handle(self, intent: Intent) -> Reply:
        action = intent.slots.get("action") if intent.action == "tool" else intent.action
        name = str(intent.slots.get("name") or "").strip()
        if action == "minimize_all":
            await run_blocking(winutil.minimize_all)
            return Reply(listen_after=False)
        if action == "list":
            windows = await run_blocking(winutil.list_windows)
            apps = list(dict.fromkeys(w.friendly for w in windows))
            if not apps:
                return Reply("Открытых окон нет.")
            return Reply("Открыто: " + ", ".join(apps[:8]) + ".")
        if action == "kill":
            return await self._kill(name)

        windows = await run_blocking(self.find, name)
        if not windows:
            if action == "close" and name:
                exe = await run_blocking(self._process_exe, name)
                if exe and not winutil.is_protected(exe):
                    return await self._kill(name, exe, "Окна у него нет, но процесс работает. Завершить принудительно?")
            return Reply(f"Не вижу окна {name}." if name else "Не вижу активного окна.")
        target = windows[0]
        if winutil.is_protected(target.exe):
            return Reply("Это системное окно, его не трогаю.")
        if action == "close":
            for w in windows:
                await run_blocking(winutil.close_window, w)
            log.info("Закрываю %s (%d окон)", target.exe, len(windows))
            return Reply(f"Закрываю {target.friendly}.", reaction="ok", tool_result="закрыто", listen_after=False)
        if action == "minimize":
            await run_blocking(winutil.minimize, target)
        elif action == "maximize":
            await run_blocking(winutil.maximize, target)
        elif action == "switch":
            await run_blocking(winutil.activate, target)
        return Reply(tool_result="готово", listen_after=False)

    async def _kill(self, name: str, exe: str = "", question: str = "") -> Reply:
        exe = exe or await run_blocking(self._process_exe, name)
        if not exe:
            return Reply(f"Не нашёл процесс {name}.")
        if winutil.is_protected(exe):
            return Reply("Это системный процесс, завершать его опасно. Не буду.")

        async def do() -> Reply:
            n = await run_blocking(winutil.kill_process_tree, exe)
            return Reply(f"Завершил {exe}." if n else f"Процесс {exe} уже не запущен.", reaction="ok" if n else "",
                         listen_after=False)

        return Reply(question or f"Завершить {exe} принудительно? Несохранённые данные пропадут.", confirm=do)


def create() -> Skill:
    return WindowsSkill()
