"""Logging: console + rotating file in data/logs. Never logs audio."""
from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

from assistant.paths import LOG_DIR


def _fix_windowless_streams() -> bool:
    """pythonw.exe has no console: sys.stdout/stderr are None and libraries crash writing to them."""
    windowless = sys.stdout is None or sys.stderr is None
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    return windowless


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    windowless = _fix_windowless_streams()
    crash = open(LOG_DIR / "crash.log", "a", encoding="utf-8")  # kept open for the process lifetime
    faulthandler.enable(crash)
    sys.excepthook = lambda t, v, tb: logging.getLogger("crash").critical("Необработанная ошибка", exc_info=(t, v, tb))
    threading.excepthook = lambda a: logging.getLogger("crash").critical(
        "Ошибка в потоке %s", a.thread.name if a.thread else "?", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    fmt = logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    if not windowless:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)

    file = RotatingFileHandler(LOG_DIR / "assistant.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    root.addHandler(file)

    for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "google_genai", "asyncio", "uvicorn.access", "openai", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
