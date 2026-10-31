"""Interruptible playback. Each clip opens a stream at its native sample rate (no resampling)."""
from __future__ import annotations

import logging
import re
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


# A Bluetooth headset's own microphone: opening it switches the headphones into the hands-free ("call") profile —
# mono sound, a volume slider stuck at 0, the buttons on the headphones no longer change the volume, and other
# programs' music cuts out when the microphone is closed again. Windows names these endpoints
# "Головной телефон (Galaxy Buds2 Hands-Free AG Audio)" / "Headset (... Hands-Free AG Audio)".
_HANDS_FREE = re.compile(r"hands-?free|\bag audio\b|^головной телефон\b|^headset\s*\(", re.I)
_MAPPER = re.compile(r"переназначение|mapper|первичный драйвер|primary sound", re.I)
last_good_input = ""   # the microphone that was in use before the headset came: kept when it connects


def is_hands_free(name: str, all_names: list[str] | None = None) -> bool:
    """MME cuts names at 31 characters ("Головной телефон (Galaxy Buds2 "): the full name decides."""
    if _HANDS_FREE.search(name):
        return True
    full = [n for n in (all_names or []) if n.startswith(name.rstrip()) and n != name]
    return any(_HANDS_FREE.search(n) for n in full)


def default_input() -> int | None:
    """The system default microphone, unless it is a Bluetooth headset's hands-free one and another microphone exists:
    then the one used before (or the first other one). None = the system default as is."""
    try:
        default = sd.query_devices(kind="input")
        devices = list(sd.query_devices())
    except Exception:
        return None
    names = [d["name"] for d in devices]
    if not is_hands_free(default["name"], names):
        return None
    others = [i for i, d in enumerate(devices)
              if d["max_input_channels"] > 0 and d["hostapi"] == default["hostapi"]
              and not is_hands_free(d["name"], names) and not _MAPPER.search(d["name"])]
    if not others:
        log.warning("Единственный микрофон — гарнитура Bluetooth «%s»: наушники перейдут в режим звонка",
                    default["name"])
        return None
    pick = next((i for i in others if devices[i]["name"] == last_good_input), others[0])
    log.info("Микрофон по умолчанию — гарнитура Bluetooth «%s»; беру «%s», чтобы наушники не ушли в режим звонка",
             default["name"], devices[pick]["name"])
    return pick


def resolve_device(spec: str, kind: str) -> int | None:
    """"" -> default (for a microphone: not a Bluetooth headset's, see default_input); digits -> index;
    otherwise substring of the device name."""
    spec = (spec or "").strip()
    if not spec:
        return default_input() if kind == "input" else None
    if spec.isdigit():
        return int(spec)
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, dev in enumerate(sd.query_devices()):
        if dev[key] > 0 and spec.lower() in dev["name"].lower():
            return i
    log.warning("Устройство «%s» не найдено, беру системное по умолчанию", spec)
    return None


class _Speech:
    """A speech clip as the output callback reads it. The volume follows Player.duck smoothly (a jump is heard as
    a click and draws attention); Player.hold pauses it at the next gap between words and it resumes from there."""

    DOWN_SEC, UP_SEC = 0.08, 0.4      # how long a full turn down / back up takes
    FADE_SEC = 0.01
    SEEK_SEC = 1.0                    # a gap between words is looked for this far ahead, else the quietest point

    def __init__(self, player: "Player", data: np.ndarray, sr: int) -> None:
        self.player, self.data, self.sr = player, data, sr
        self.pos = 0
        self.paused = False
        self._cut: int | None = None
        self._blk = max(1, sr // 100)
        n = len(data) // self._blk
        self._rms = np.sqrt(np.mean(data[:n * self._blk].reshape(n, self._blk) ** 2, axis=1)) if n else np.zeros(0)
        self._quiet = 0.15 * float(np.percentile(self._rms, 95)) if n else 0.0

    def _gap(self) -> int:
        """Where to stop for a pause: the next quiet 10 ms (between words), or the quietest point within a second."""
        b = self.pos // self._blk + 1
        window = self._rms[b:b + int(self.SEEK_SEC * 100)]
        if not len(window):
            return len(self.data)
        quiet = np.flatnonzero(window <= self._quiet)
        i = int(quiet[0]) if len(quiet) else int(np.argmin(window))
        return min(len(self.data), (b + i) * self._blk + self._blk // 2)

    def read(self, frames: int) -> tuple[np.ndarray, bool]:
        """The next `frames` samples (silence while paused) and whether the clip is over."""
        out = np.zeros(frames, np.float32)
        player = self.player
        resumed = False
        if self.paused:
            if player.hold:
                return out, False
            self.paused, resumed = False, True
        end = min(self.pos + frames, len(self.data))
        stop_here = False
        if player.hold:
            if self._cut is None:
                self._cut = self._gap()
            if self._cut < len(self.data) and self._cut <= end:
                end, stop_here = max(self._cut, self.pos), True
        else:
            self._cut = None
        chunk = self.data[self.pos:end].copy()
        n = len(chunk)
        if n:
            g0, target = player.gain, max(0.0, min(1.0, player.duck))
            step = n / (self.sr * (self.DOWN_SEC if target < g0 else self.UP_SEC))
            g1 = g0 + max(-step, min(step, target - g0))
            if g0 < 1.0 or g1 < 1.0:
                chunk *= np.linspace(g0, g1, n, dtype=np.float32)
            player.gain = g1
            f = min(n, int(self.FADE_SEC * self.sr))
            if resumed:
                chunk[:f] *= np.linspace(0.0, 1.0, f, dtype=np.float32)
            if stop_here:
                chunk[n - f:] *= np.linspace(1.0, 0.0, f, dtype=np.float32)
        out[:n] = chunk
        self.pos = end
        if stop_here:
            self.paused, self._cut = True, None
            return out, False
        return out, self.pos >= len(self.data)


class Player:
    def __init__(self, device: str = "", volume: float = 1.0, on_level: Callable[[float], None] | None = None) -> None:
        self.device_spec = device
        self.device = resolve_device(device, "output")
        self.volume = volume
        self.duck = 1.0          # live share of the volume: lowered while the user talks over Jarvis
        self.gain = 1.0          # the share applied right now: follows `duck` smoothly
        self.hold = False        # speech pauses at the next gap between words until this is cleared
        self.on_level = on_level
        self._stop = threading.Event()
        self._lock = threading.Lock()  # one speech clip at a time

    def stop(self) -> None:
        self._stop.set()

    def release(self) -> None:
        """Full volume, no pause (the answer is over or the user is done)."""
        self.duck = 1.0
        self.hold = False

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
        speech = _Speech(self, data, clip.sr) if duckable else None
        pos = 0
        done = threading.Event()
        interrupted = False

        def callback(outdata, frames, _time, _status):
            nonlocal pos, interrupted
            if stop.is_set():
                interrupted = True
                outdata.fill(0)
                raise sd.CallbackStop
            if speech is not None:
                out, over = speech.read(frames)
                outdata[:, 0] = out
                ref.write(out, clip.sr)
                if on_level is not None:
                    on_level(float(np.sqrt(np.mean(out * out))))
                if over:
                    raise sd.CallbackStop
                return
            chunk = data[pos:pos + frames]
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
