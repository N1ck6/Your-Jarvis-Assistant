"""Interruptible playback. Each clip opens a stream at its native sample rate (no resampling)."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd

log = logging.getLogger("player")


@dataclass
class AudioClip:
    samples: np.ndarray  # float32 mono, -1..1
    sr: int

    @property
    def seconds(self) -> float:
        return len(self.samples) / self.sr


def resolve_device(spec: str, kind: str) -> int | None:
    """"" -> default; digits -> index; otherwise substring of the device name."""
    spec = (spec or "").strip()
    if not spec:
        return None
    if spec.isdigit():
        return int(spec)
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, dev in enumerate(sd.query_devices()):
        if dev[key] > 0 and spec.lower() in dev["name"].lower():
            return i
    log.warning("Устройство «%s» не найдено, беру системное по умолчанию", spec)
    return None


class Player:
    def __init__(self, device: str = "", volume: float = 1.0, on_level: Callable[[float], None] | None = None) -> None:
        self.device = resolve_device(device, "output")
        self.volume = volume
        self.on_level = on_level
        self._stop = threading.Event()
        self._lock = threading.Lock()  # one speech clip at a time

    def stop(self) -> None:
        self._stop.set()

    def play(self, clip: AudioClip) -> bool:
        """Blocks until the clip ends. Returns False if interrupted."""
        with self._lock:
            self._stop.clear()
            return self._run(clip, self._stop, self.volume, self.on_level)

    def effect(self, clip: AudioClip, volume: float) -> None:
        """Fire-and-forget sound (earcons); does not interrupt speech."""
        threading.Thread(target=self._run, args=(clip, threading.Event(), volume, None), daemon=True).start()

    def _run(self, clip: AudioClip, stop: threading.Event, volume: float, on_level) -> bool:
        data = np.clip(clip.samples.astype(np.float32) * volume, -1.0, 1.0)
        pos = 0
        done = threading.Event()
        interrupted = False

        def callback(outdata, frames, _time, _status):
            nonlocal pos, interrupted
            if stop.is_set():
                interrupted = True
                outdata.fill(0)
                raise sd.CallbackStop
            chunk = data[pos:pos + frames]
            n = len(chunk)
            outdata[:n, 0] = chunk
            if n < frames:
                outdata[n:, 0] = 0
            pos += n
            if on_level is not None and n:
                on_level(float(np.sqrt(np.mean(chunk * chunk))))
            if n < frames:
                raise sd.CallbackStop

        try:
            with sd.OutputStream(samplerate=clip.sr, channels=1, dtype="float32", device=self.device,
                                 callback=callback, finished_callback=done.set):
                while not done.wait(0.05):
                    if stop.is_set():
                        break
        except sd.PortAudioError as exc:
            log.error("Ошибка вывода звука: %s", exc)
            return False
        if on_level is not None:
            on_level(0.0)
        return not (interrupted or stop.is_set())
