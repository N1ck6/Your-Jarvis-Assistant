"""Update check: once a day compare the local version with GitHub (github.com/N1ck6/Your-Jarvis-Assistant). Only a notification;
nothing is downloaded or installed. A git checkout compares commits, an installed build compares release tags."""
from __future__ import annotations

import json
import logging
import subprocess
import time

import httpx

from assistant import __version__
from assistant.paths import DATA_DIR, ROOT

log = logging.getLogger("updates")
REPO = "N1ck6/Your-Jarvis-Assistant"
STATE = DATA_DIR / "update_check.json"
DAY = 24 * 3600


def _local_commit() -> str:
    if not (ROOT / ".git").exists():
        return ""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())


async def check(force: bool = False) -> str | None:
    """A short description of what is new, or None (up to date, checked recently, offline)."""
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not force and time.time() - state.get("checked", 0) < DAY:
        return None
    news = None
    try:
        async with httpx.AsyncClient(timeout=10, headers={"Accept": "application/vnd.github+json"}) as client:
            commit = _local_commit()
            if commit:
                r = await client.get(f"https://api.github.com/repos/{REPO}/compare/{commit}...main")
                if r.status_code == 200 and r.json().get("ahead_by", 0) > 0:
                    data = r.json()
                    first = data["commits"][-1]["commit"]["message"].splitlines()[0].lstrip("- ")
                    news = f"новых изменений: {data['ahead_by']}. Последнее: {first}"
            else:
                r = await client.get(f"https://api.github.com/repos/{REPO}/releases/latest")
                if r.status_code == 200 and _version_tuple(r.json().get("tag_name", "")) > _version_tuple(__version__):
                    news = f"версия {r.json()['tag_name']}"
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.debug("Проверка обновлений: %s", exc)
        return None
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"checked": time.time(), "news": news}, ensure_ascii=False), encoding="utf-8")
    if news:
        log.info("Доступно обновление: %s", news)
    return news
