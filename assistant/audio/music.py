"""Built-in music player (miniaudio): mp3/flac/wav/ogg without external apps or playlist files.

Supports pause/resume/next/prev/stop and ducking: music gets quieter while Jarvis listens or speaks.
"""
from __future__ import annotations

import array
import logging
import threading
from pathlib import Path

import miniaudio
import numpy as np

log = logging.getLogger("music")

SR = 44100
CHANNELS = 2


class MusicPlayer:
    def __init__(self, volume: float = 0.6, duck_volume: float = 0.2) -> None:
        self.volume = volume
        self.duck_volume = duck_volume
        self.queue: list[Path] = []
        self.index = -1
        self.state = "stopped"          # stopped | playing | paused
        self._ducked = False
        self._stream = None
        self._lock = threading.RLock()
        self._device: miniaudio.PlaybackDevice | None = None

    # ------------------------------------------------------------------ public API
    @property
    def active(self) -> bool:
        return self.state != "stopped"

    @property
    def current(self) -> Path | None:
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    def play(self, queue: list[Path], start: int = 0) -> None:
        with self._lock:
            self.queue = list(queue)
            self._open(start)
            self.state = "playing"
        self._ensure_device()

    def pause(self) -> None:
        with self._lock:
            if self.state == "playing":
                self.state = "paused"

    def resume(self) -> None:
        with self._lock:
            if self.state == "paused":
                self.state = "playing"

    def toggle(self) -> None:
        self.pause() if self.state == "playing" else self.resume()

    def next(self) -> None:
        with self._lock:
            if self.queue:
                self._open((self.index + 1) % len(self.queue))
                self.state = "playing"

    def prev(self) -> None:
        with self._lock:
            if self.queue:
                self._open((self.index - 1) % len(self.queue))
                self.state = "playing"

    def stop(self) -> None:
        with self._lock:
            self.state = "stopped"
            self._stream = None
        if self._device is not None:
            self._device.close()
            self._device = None

    def duck(self, on: bool) -> None:
        self._ducked = on

    # ------------------------------------------------------------------ internals
    def _open(self, index: int) -> None:
        self.index = index
        path = self.queue[index]
        try:
            self._stream = miniaudio.stream_file(str(path), output_format=miniaudio.SampleFormat.FLOAT32,
                                                 nchannels=CHANNELS, sample_rate=SR, frames_to_read=1024)
            next(self._stream)  # prime the generator
            log.info("Играет: %s", path.stem)
        except (miniaudio.DecodeError, OSError) as exc:
            log.warning("Не могу проиграть %s: %s", path.name, exc)
            self._stream = None

    def _ensure_device(self) -> None:
        if self._device is not None:
            return
        self._device = miniaudio.PlaybackDevice(output_format=miniaudio.SampleFormat.FLOAT32, nchannels=CHANNELS,
                                                sample_rate=SR, buffersize_msec=120, app_name="Jarvis music")
        gen = self._generator()
        next(gen)
        self._device.start(gen)

    def _generator(self):
        required = yield b""
        while True:
            with self._lock:
                chunk = self._pull(required) if self.state == "playing" else None
            if chunk is None:
                chunk = array.array("f", bytes(required * CHANNELS * 4))
            required = yield chunk

    def _pull(self, frames: int):
        """Next audio chunk scaled by volume; advances to the next track at the end of the file."""
        for _ in range(len(self.queue) + 1):
            if self._stream is not None:
                try:
                    data = self._stream.send(frames)
                    if len(data):
                        gain = self.volume * (self.duck_volume if self._ducked else 1.0)
                        out = np.frombuffer(data, dtype=np.float32) * gain
                        return array.array("f", out.astype(np.float32).tobytes())
                except StopIteration:
                    pass
            if not self.queue:
                break
            if self.index + 1 >= len(self.queue):
                self.state = "stopped"  # end of the queue
                self._stream = None
                return None
            self._open(self.index + 1)
        return None
