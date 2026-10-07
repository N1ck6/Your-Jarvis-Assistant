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


def av_stream(path: Path):
    """The miniaudio stream protocol (prime with next(), then send(frames) -> float32 array) on top of PyAV/FFmpeg."""
    import av

    container = av.open(str(path))
    try:
        resampler = av.AudioResampler(format="flt", layout="stereo", rate=SR)
        buf = np.zeros(0, dtype=np.float32)
        frames = yield array.array("f")
        for decoded in container.decode(audio=0):
            for out in resampler.resample(decoded):
                buf = np.concatenate([buf, out.to_ndarray().reshape(-1)])
                while len(buf) >= frames * CHANNELS:
                    chunk, buf = buf[:frames * CHANNELS], buf[frames * CHANNELS:]
                    frames = yield array.array("f", chunk.tobytes())
        for out in resampler.resample(None):
            buf = np.concatenate([buf, out.to_ndarray().reshape(-1)])
        while len(buf):
            chunk, buf = buf[:frames * CHANNELS], buf[frames * CHANNELS:]
            frames = yield array.array("f", chunk.tobytes())
    finally:
        container.close()


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
        from assistant.audio.aec import REFERENCE

        self._ref = REFERENCE.stream()   # what the music sends to the speakers, for the echo canceller

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

    def reopen(self) -> None:
        """New default speakers (headphones plugged in): reopen the output, keep the track and position."""
        with self._lock:
            device, self._device = self._device, None
        if device is None:
            return
        device.close()
        if self.active:
            self._ensure_device()
            log.info("Музыка переключена на новое устройство")

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
            # Downloads are often AAC/MP4 named .mp3; miniaudio reads only mp3/flac/wav/ogg.
            try:
                self._stream = av_stream(path)
                next(self._stream)
                log.info("Играет (через FFmpeg): %s", path.stem)
            except Exception as exc2:  # PyAV missing or the file is broken
                log.warning("Не могу проиграть %s: %s / %s", path.name, exc, exc2)
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
                        self._ref.write(out.reshape(-1, CHANNELS).mean(axis=1), SR)   # for the echo canceller
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
