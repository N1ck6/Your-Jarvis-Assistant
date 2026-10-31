"""Restart on new code: a new commit in Jarvis's folder (an edit committed, or a `git pull`) restarts him by himself
when he is idle, silently, instead of the user quitting from the tray and starting run.bat again.

The new code is checked first in a separate process (it compiles and imports): a broken commit leaves the running
Jarvis as he is and says so in a notification, rather than leaving the user with no Jarvis at all.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("selfupdate")

_CHECK = ("import compileall, sys; ok = compileall.compile_dir('assistant', quiet=1); "
          "import assistant.app, assistant.assistant; sys.exit(0 if ok else 1)")


def head(root: Path) -> str | None:
    """The current commit of the folder, read from .git without running git; None if it is not a git checkout."""
    git = root / ".git"
    try:
        ref = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not ref.startswith("ref:"):
        return ref                                  # a detached HEAD holds the commit itself
    name = ref[4:].strip()
    try:
        return (git / name).read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:                                            # after `git gc` the branch lives in packed-refs
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            sha, _, ref_name = line.partition(" ")
            if ref_name == name:
                return sha
    except OSError:
        pass
    return None


def subject(root: Path) -> str:
    """The first line of the current commit's message (for the notification), or ""."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=root, capture_output=True, text=True,
                             encoding="utf-8", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def python(root: Path) -> str:
    exe = root / ".venv" / "Scripts" / "python.exe"
    return str(exe) if exe.exists() else sys.executable


def check(root: Path) -> str:
    """"" if the new code compiles and imports, else the end of the error."""
    try:
        out = subprocess.run([python(root), "-c", _CHECK], cwd=root, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=180,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if out.returncode == 0:
        return ""
    lines = [x for x in (out.stderr or out.stdout).strip().splitlines() if x.strip()]
    return lines[-1] if lines else f"код возврата {out.returncode}"
