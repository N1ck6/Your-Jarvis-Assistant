"""Interruptible playback. Each clip opens a stream at its native sample rate (no resampling)."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd

log = logging.getLogger("player")


class _StreamGate:
    """Many streams may be open at once; re-reading the device list needs none of them open."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._open = 0
        self._reinit = False

    def __enter__(self) -> None:
        with self._cond:
            while self._reinit:
                self._cond.wait()
            self._open += 1

    def __exit__(self, *_exc) -> None:
        with self._cond:
            self._open -= 1
            self._cond.notify_all()

    def reinit(self, timeout: float = 15.0) -> bool:
        """Makes PortAudio see new devices (headphones, a new default). Waits for open streams to close."""
        with self._cond:
            self._reinit = True
            try:
                if not self._cond.wait_for(lambda: self._open == 0, timeout):
                    return False
                sd._terminate()
                sd._initialize()
                return True
            finally:
                self._reinit = False
                self._cond.notify_all()


GATE = _StreamGate()


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
        self.device_spec = device
        self.device = resolve_device(device, "output")
        self.volume = volume
        self.duck = 1.0          # live share of the volume: lowered while the user talks over Jarvis
        self.on_level = on_level
        self._stop = threading.Event()
        self._lock = threading.Lock()  # one speech clip at a time

    def stop(self) -> None:
        self._stop.set()

    def refresh_device(self) -> None:
        """After PortAudio re-read the devices, indexes may have moved: resolve the configured one again."""
        self.device = resolve_device(self.device_spec, "output")

    def play(self, clip: AudioClip) -> bool:
        """Blocks until the clip ends. Returns False if interrupted."""
        with self._lock:
            self._stop.clear()
            return self._run(clip, self._stop, self.volume, self.on_level, duckable=True)

    def effect(self, clip: AudioClip, volume: float) -> None:
        """Fire-and-forget sound (earcons); does not interrupt speech."""
        threading.Thread(target=self._run, args=(clip, threading.Event(), volume, None), daemon=True).start()

    def _run(self, clip: AudioClip, stop: threading.Event, volume: float, on_level, duckable: bool = False) -> bool:
        """duckable: speech, turned down while the user talks over it; cues keep their volume."""
        from assistant.audio.aec import REFERENCE

        data = np.clip(clip.samples.astype(np.float32) * volume, -1.0, 1.0)
        ref = REFERENCE.stream()   # what goes to the speakers, for the echo canceller
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
            if duckable and self.duck < 1.0:
                chunk = chunk * self.duck
            n = len(chunk)
            outdata[:n, 0] = chunk
            if n:
                ref.write(chunk, clip.sr)
            if n < frames:
                outdata[n:, 0] = 0
            pos += n
            if on_level is not None and n:
                on_level(float(np.sqrt(np.mean(chunk * chunk))))
            if n < frames:
                raise sd.CallbackStop

        try:
            with GATE, sd.OutputStream(samplerate=clip.sr, channels=1, dtype="float32", device=self.device,
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
