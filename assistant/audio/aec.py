"""Echo cancellation: the microphone stops hearing Jarvis himself.

Everything Jarvis plays (speech, cues, music) is copied here as the reference; WebRTC AEC3 (the echo canceller of
web browsers, through the livekit package) subtracts it from the microphone. Without it the microphone hears
Jarvis's voice from the speakers as loud as the user's: "Джарвис, …" said over an answer was missed a third of the
time and the command after it misheard (scripts/bargein_bench.py).

Timing. Reference and microphone are laid on one clock of 10 ms blocks: a player stream starts at the block of "now"
when its first chunk is written and continues block by block (no callback jitter inside a stream); several streams
playing at once are summed. The microphone counts its own blocks from its first frame and takes the reference
block with the same number. Audio reaches the speakers a little after it is written, so the reference always
leads the echo, which is what AEC3 expects; it finds the exact delay by itself.
"""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger("aec")

SR = 16000
CHUNK = SR // 100           # 10 ms: the frame size of the WebRTC audio processing
KEEP_BLOCKS = 300           # reference kept for 3 s (nothing reads it while the microphone is muted)


def to_16k(samples: np.ndarray, sr: int) -> np.ndarray:
    if sr == SR:
        return samples.astype(np.float32)
    n = int(round(len(samples) * SR / sr))
    if n <= 0:
        return np.zeros(0, np.float32)
    return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(np.float32)


# The clock both sides use; a benchmark replaying audio faster than real time sets its own.
clock = time.monotonic


def _now_block() -> int:
    return int(clock() * 100)


class Reference:
    """What the speakers play, as 16 kHz mono 10 ms blocks on the monotonic clock; written by audio callbacks."""

    def __init__(self) -> None:
        self._blocks: dict[int, np.ndarray] = {}
        self._lock = threading.Lock()
        self.played_sec = 0.0          # how much has been played since start: the canceller adapts on it

    def stream(self) -> "RefStream":
        return RefStream(self)

    def _add(self, block: int, samples: np.ndarray) -> None:
        with self._lock:
            cur = self._blocks.get(block)
            self._blocks[block] = samples.copy() if cur is None else cur + samples
            if len(self._blocks) > KEEP_BLOCKS * 2:
                oldest = block - KEEP_BLOCKS
                for k in [k for k in self._blocks if k < oldest]:
                    del self._blocks[k]

    def take(self, block: int) -> np.ndarray:
        with self._lock:
            out = self._blocks.pop(block, None)
            for k in [k for k in self._blocks if k < block - 20]:   # skipped by a late microphone
                del self._blocks[k]
        return out if out is not None else np.zeros(CHUNK, np.float32)

    def clear(self) -> None:
        with self._lock:
            self._blocks.clear()


class RefStream:
    """One playing stream (a speech clip, a cue, the music): consecutive chunks land in consecutive blocks."""

    def __init__(self, ref: Reference) -> None:
        self.ref = ref
        self._block: int | None = None
        self._carry = np.zeros(0, np.float32)

    def write(self, samples: np.ndarray, sr: int) -> None:
        data = to_16k(np.asarray(samples, np.float32), sr)
        if self._block is None or self._block < _now_block() - 5:   # first chunk, or the stream stalled
            self._block = _now_block()
            self._carry = np.zeros(0, np.float32)
        data = np.concatenate([self._carry, data])
        n = len(data) // CHUNK
        for j in range(n):
            self.ref._add(self._block, data[j * CHUNK:(j + 1) * CHUNK])
            self._block += 1
        self._carry = data[n * CHUNK:]
        self.ref.played_sec += n / 100


REFERENCE = Reference()


class EchoCanceller:
    """16 kHz int16 microphone frames in, echo-free frames of the same length out (under 10 ms later)."""

    def __init__(self, reference: Reference = REFERENCE, noise_suppression: bool = False) -> None:
        from livekit import rtc

        self._rtc = rtc
        self._apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=noise_suppression,
                                              high_pass_filter=True)
        self.reference = reference
        self._block: int | None = None        # the clock block of the next 10 ms of microphone audio
        self._in = np.zeros(0, np.int16)
        self._out = np.zeros(0, np.int16)

    @property
    def adapted(self) -> bool:
        """The canceller has heard enough of Jarvis to know the room (the first second it lets echo through)."""
        return self.reference.played_sec >= 3.0

    def restart(self) -> None:
        """The microphone was reopened: the next frame starts a new count."""
        self._block = None
        self._in = np.zeros(0, np.int16)
        self._out = np.zeros(0, np.int16)

    def process(self, frame: np.ndarray, captured_at: float | None = None) -> np.ndarray:
        """frame: int16 microphone samples; captured_at: monotonic time of its first sample (None = now)."""
        if self._block is None:
            t = clock() if captured_at is None else captured_at
            self._block = int(t * 100)
        elif captured_at is not None and abs(captured_at * 100 - (self._block + self._in.size / CHUNK)) > 20:
            self._block = int(captured_at * 100)      # the microphone stalled or the clocks drifted: re-anchor
            self._in = np.zeros(0, np.int16)
        self._in = np.concatenate([self._in, frame.astype(np.int16)])
        done = []
        while self._in.size >= CHUNK:
            mic, self._in = self._in[:CHUNK], self._in[CHUNK:]
            ref = (np.clip(self.reference.take(self._block), -1, 1) * 32767).astype(np.int16)
            self._block += 1
            self._apm.process_reverse_stream(self._rtc.AudioFrame(ref.tobytes(), SR, 1, CHUNK))
            cap = self._rtc.AudioFrame(mic.tobytes(), SR, 1, CHUNK)
            self._apm.process_stream(cap)
            done.append(np.frombuffer(bytes(cap.data), dtype=np.int16))
        if done:
            self._out = np.concatenate([self._out, *done])
        n = frame.size
        if self._out.size < n:   # the very first frame: the start is padded with silence
            self._out = np.concatenate([np.zeros(n - self._out.size, np.int16), self._out])
        out, self._out = self._out[:n], self._out[n:]
        return out


def create() -> EchoCanceller | None:
    try:
        return EchoCanceller()
    except Exception as exc:  # livekit missing or its native library failed to load
        log.warning("Эхоподавление недоступно (%s): микрофон слышит и речь Джарвиса", exc)
        return None
