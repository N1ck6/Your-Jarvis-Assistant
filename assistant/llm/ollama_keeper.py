"""Keeps the local model fast: Ollama running, the model whole in video memory, a smaller one when memory is short.

Why. Ollama places a model when it loads it. If another program holds the video memory at that moment (a game,
a video or voice project), most layers go to the CPU: the router then takes 2-5 s instead of ~0.4 s, and stays that
slow for as long as the model is kept loaded (every request renews keep_alive), even after the GPU is free again.
So the model is reloaded once enough video memory is free, while Jarvis is idle.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("llm")

# Smaller models to fall back to when the configured one does not fit in memory, best first.
SMALLER = ["qwen3:4b", "qwen3:1.7b", "qwen3:0.6b"]
WHOLE = 0.95       # share of the model in video memory that counts as "on the GPU"
MARGIN_MB = 400    # free video memory needed above the model's missing part before a reload


def start_server() -> bool:
    """Starts Ollama if it is installed (the tray app, else `ollama serve`). True if something was started."""
    app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if app.exists():
            subprocess.Popen([str(app)], creationflags=flags, close_fds=True)
            return True
        exe = shutil.which("ollama")
        if exe:
            subprocess.Popen([exe, "serve"], creationflags=flags, close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except OSError as exc:
        log.warning("Ollama не запустилась: %s", exc)
    return False


def gpu_free_mb() -> float | None:
    """Free video memory of the first NVIDIA GPU; None without nvidia-smi (another vendor, no driver tools)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return float(out.stdout.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def worth_reload(size: int, size_vram: int, free_mb: float | None) -> bool:
    """The model is partly on the CPU and the part that is missing on the GPU fits there now."""
    if size <= 0 or size_vram / size >= WHOLE:
        return False
    if free_mb is None:
        return True   # cannot tell: try (rarely, the caller limits how often)
    return free_mb >= (size - size_vram) / 2**20 + MARGIN_MB


def smaller_model(installed: list[str], current: str) -> str | None:
    """The best installed fallback that is not the current model."""
    names = {n.removesuffix(":latest") for n in installed}
    after = SMALLER[SMALLER.index(current) + 1:] if current in SMALLER else SMALLER
    return next((m for m in after if m in names), None)
