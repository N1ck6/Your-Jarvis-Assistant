"""Restart on new code: a new commit in Jarvis's folder (an edit committed, or a `git pull`) restarts him by himself,
silently, instead of the user quitting from the tray and starting run.bat again.

1. The new code is checked at once in a separate process (it compiles and imports) while Jarvis keeps working; the
   compiled files stay, so the new instance starts faster. A broken commit leaves him as he is (a notification).
2. When he is idle, the new instance starts next to him and loads its models (~5-8 s) while he still answers.
3. It says it is ready (data/handover.ready); once he is idle again he exits, and it takes the microphone, the tray
   and the hotkeys within a second.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from assistant.paths import DATA_DIR

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


# --- handover: the new Jarvis loads next to the running one and takes over when it exits ---
READY = DATA_DIR / "handover.ready"


def spawn(root: Path, quiet: bool, updated: bool) -> subprocess.Popen:
    """The new instance in handover mode: it loads everything, says it is ready, then waits for the lock."""
    scripts = root / ".venv" / "Scripts"
    exe = scripts / "Jarvis.exe"  # assistant/named_exe.py: Task Manager shows "Jarvis", not "Python"
    if not exe.exists():
        exe = scripts / "pythonw.exe"
    cmd = [str(exe) if exe.exists() else sys.executable, "-m", "assistant", "--handover"]
    cmd += ["--quiet"] * quiet + ["--updated"] * updated
    env = {**os.environ, "JARVIS_HANDOVER_FROM": str(os.getpid())}
    return subprocess.Popen(cmd, cwd=str(root), env=env, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def spawned_pid() -> int:
    """The pid the old Jarvis got from Popen. The venv's pythonw.exe is a launcher that runs the real interpreter as
    its child, so behind it the old Jarvis knows the launcher (our parent), not us; Jarvis.exe is one process."""
    started_by = os.environ.get("JARVIS_HANDOVER_FROM", "")
    return os.getpid() if started_by == str(os.getppid()) else os.getppid()


def mark_ready() -> None:
    READY.parent.mkdir(parents=True, exist_ok=True)
    READY.write_text(str(spawned_pid()), encoding="utf-8")


def kill(proc: subprocess.Popen) -> None:
    """The launcher and the interpreter it started."""
    import psutil

    try:
        for child in psutil.Process(proc.pid).children(recursive=True):
            child.kill()
    except psutil.Error:
        pass
    proc.kill()


def is_ready(pid: int) -> bool:
    try:
        return READY.read_text(encoding="utf-8").strip() == str(pid)
    except OSError:
        return False


def clear() -> None:
    try:
        READY.unlink()
    except OSError:
        pass
