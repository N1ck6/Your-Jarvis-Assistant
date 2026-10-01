"""Start with Windows: a shortcut in the user's Startup folder (no registry, no admin rights, easy to remove by hand)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from assistant.paths import ROOT

log = logging.getLogger("autostart")
NAME = "Джарвис.lnk"


def startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path(folder: Path | None = None) -> Path:
    return (folder or startup_dir()) / NAME


def is_enabled(folder: Path | None = None) -> bool:
    return shortcut_path(folder).exists()


def _target() -> tuple[str, str]:
    """(program, arguments): the venv's windowless Python, or the frozen exe of an installed build."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    return str(pythonw if pythonw.exists() else Path(sys.executable).with_name("pythonw.exe")), "-m assistant"


def set_enabled(enabled: bool, folder: Path | None = None) -> None:
    path = shortcut_path(folder)
    if not enabled:
        path.unlink(missing_ok=True)
        log.info("Автозапуск выключен")
        return
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    program, args = _target()
    link = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(path))
    link.TargetPath = program
    link.Arguments = args
    link.WorkingDirectory = str(ROOT if not getattr(sys, "frozen", False) else Path(sys.executable).parent)
    link.Description = "Джарвис — голосовой ассистент"
    link.Save()
    log.info("Автозапуск включён: %s", path)
