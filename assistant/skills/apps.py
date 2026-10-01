"""Open applications and websites by voice. Index = Windows Start menu (Get-StartApps) + config aliases."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import webbrowser
from pathlib import Path

from rapidfuzz import fuzz, process

from assistant.skills.base import Intent, Reply, Skill, Tool, run_blocking

log = logging.getLogger("apps")

_OPEN = re.compile(
    r"^(?:открой|открыть|открывай|запусти|запустить|запускай|включи|включить|вруби|врубай|стартуй|загрузи|"
    r"зайди в|зайди на|перейди на сайт|перейди на|хочу поиграть в|давай поиграем в|поиграем в|го в|погнали в)\s+"
    r"(?:мне\s+)?(?:приложение|программу|программа|сайт|игру)?\s*(?P<name>.+)$"
)
_DOMAIN = re.compile(r"^[\w-]+(\.[\w-]+)+$")
CYR2LAT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})
# Media words that follow "включи" but mean something else.
_NOT_APPS = {"музыку", "свет", "микрофон", "звук", "таймер", "таймеры", "помодоро", "диктовку", "окно"}


class AppsSkill(Skill):
    name = "apps"
    title = "Запуск приложений и сайтов"
    examples = [
        'открой <приложение или сайт>',
    ]

    def __init__(self) -> None:
        self.index: dict[str, str] = {}   # lowercase name -> launch target

    async def start(self) -> None:
        self.index = await run_blocking(self._build_index)
        log.info("Приложений в индексе: %d", len(self.index))

    @staticmethod
    def _build_index() -> dict[str, str]:
        index: dict[str, str] = {}
        folders = [Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
                   Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs"]
        for folder in folders:
            if folder.exists():
                for p in folder.rglob("*"):
                    if p.suffix.lower() in (".lnk", ".url", ".appref-ms"):
                        index.setdefault(p.stem.lower(), str(p))
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"],
                capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for app in json.loads(out.stdout.decode("utf-8") or "[]"):
                index.setdefault(app["Name"].lower(), "shell:AppsFolder\\" + app["AppID"])
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.warning("Get-StartApps недоступен: %s", exc)
        noise = ("uninstall", "удалить", "readme", "help", "справка", "documentation", "website")
        return {k: v for k, v in index.items() if not any(n in k for n in noise)}

    def match(self, text: str) -> Intent | None:
        m = _OPEN.match(text.strip(" .!?"))
        if not m:
            return None
        name = re.split(r"[,;]|пожалуйста|чтобы", m.group("name"))[0].strip(" .!?")
        if name in _NOT_APPS:
            return None
        return Intent(self.name, "open", {"name": name}, text)

    def tools(self) -> list[Tool]:
        return [Tool("open_app", "Открыть приложение, игру или сайт на компьютере пользователя.",
                     {"type": "object", "properties": {"name": {"type": "string", "description": "Название"}},
                      "required": ["name"]}, "open")]

    def resolve(self, name: str) -> tuple[str, str] | None:
        """Returns (display name, target)."""
        cfg = self.app.cfg.apps
        low = name.lower().strip()
        if low in ("браузер", "интернет"):
            return "браузер", cfg.browser_home
        aliases = {k.lower(): v for k, v in cfg.aliases.items()}
        hit = process.extractOne(low, aliases.keys(), scorer=fuzz.ratio, score_cutoff=85)
        if hit:
            return hit[0], aliases[hit[0]]
        if _DOMAIN.match(low):
            return low, "https://" + low
        candidates = [low, cfg.translit.get(low, ""), low.translate(CYR2LAT)]
        tr = process.extractOne(low, cfg.translit.keys(), scorer=fuzz.ratio, score_cutoff=85)
        if tr:
            candidates.insert(1, cfg.translit[tr[0]])
        for cand in filter(None, candidates):
            if cand in self.index:
                return cand, self.index[cand]
            best = process.extractOne(cand, self.index.keys(), scorer=fuzz.WRatio, score_cutoff=86)
            if best:
                return best[0], self.index[best[0]]
        return None

    async def handle(self, intent: Intent) -> Reply:
        name = str(intent.slots.get("name", "")).strip()
        if not name:
            return Reply("Что открыть?")
        found = self.resolve(name)
        if not found:
            return Reply(f"Не нашёл «{name}» среди приложений.", tool_result=f"Приложение {name} не найдено",
                         fallthrough=True)
        title, target = found
        try:
            await run_blocking(_launch, target)
        except OSError as exc:
            log.warning("Не удалось открыть %s: %s", target, exc)
            return Reply(f"Не получилось открыть {title}.")
        log.info("Открываю %s -> %s", title, target)
        return Reply(f"Открываю {title}.", reaction="ok", tool_result=f"открыто {title}", listen_after=False)


def _launch(target: str) -> None:
    if target.startswith(("http://", "https://")):
        webbrowser.open(target)
    elif target.startswith("shell:AppsFolder"):
        subprocess.Popen(["explorer.exe", target], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        os.startfile(target)  # type: ignore[attr-defined]


def create() -> Skill:
    return AppsSkill()
