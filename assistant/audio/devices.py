"""Follows audio device changes: headphones plugged in, the default device switched in Windows, a USB mic removed.

PortAudio (sounddevice) reads the device list once, so after a change the microphone keeps listening to a dead
device and new speech goes to the old speakers. The watcher polls the default endpoints (two cheap COM calls
every 2 s) and asks the assistant to reopen its streams.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger("devices")


def default_ids() -> tuple[str, str]:
    """(default playback id, default recording id) from Windows Core Audio."""
    from pycaw.pycaw import AudioUtilities

    out = ""
    mic = ""
    try:
        out = AudioUtilities.GetSpeakers().id or ""
    except Exception:
        pass
    try:
        mic = AudioUtilities.GetMicrophone().GetId() or ""
    except Exception:
        pass
    return out, mic


class DeviceWatcher:
    def __init__(self, on_change: Callable[[set[str]], None], interval: float = 2.0,
                 probe: Callable[[], tuple[str, str]] = default_ids) -> None:
        self.on_change = on_change
        self.interval = interval
        self.probe = probe
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="audio-devices", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass
        last = self.probe()
        while not self._stop.wait(self.interval):
            try:
                now = self.probe()
            except Exception:
                continue
            changed = {kind for kind, a, b in (("output", last[0], now[0]), ("input", last[1], now[1])) if a != b and b}
            last = now
            if changed:
                log.info("Сменилось аудиоустройство: %s", ", ".join(sorted(changed)))
                try:
                    self.on_change(changed)
                except Exception:
                    log.exception("Переключение аудиоустройства")
