"""Microphone state machine: VAD gate -> wake word -> utterance capture.

Audio lives only in small in-memory buffers; nothing is written to disk.

Modes
  WAIT     idle; VAD runs on every frame, Vosk only while someone speaks
  CAPTURE  recording a command until end-of-speech silence
  AWAIT    ready for a command without the wake word (hot window, bare wake, hotkey)
  DICTATE  recording dictation (until the hotkey is released or, by voice, until a pause)
  PAUSED   microphone stream closed (muted)
"""
from __future__ import annotations

import collections
import enum
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd

from assistant.audio.player import GATE, resolve_device
from assistant.audio.vad import FRAME, SileroVad
from assistant.audio.wake import VoskWake

log = logging.getLogger("listener")


class Mode(str, enum.Enum):
    WAIT = "wait"
    CAPTURE = "capture"
    AWAIT = "await"
    DICTATE = "dictate"
    PAUSED = "paused"


@dataclass
class Utterance:
    audio: np.ndarray   # float32 16 kHz mono
    source: str         # "wake" | "await" (bare wake, hotkey, after "стоп") | "hot" (window after an answer) | "dictation"
    voiced_sec: float = 0.0   # how much of it the VAD called speech
    barge_in: bool = False    # the wake word came while Jarvis was talking (his voice is in the audio)


@dataclass
class ListenerEvents:
    on_wake: Callable[[], None]
    # (word, recent audio float32 16 kHz): the word is only a hint, the assistant verifies it with STT
    on_stop_word: Callable[[str, np.ndarray], None]
    on_speech_start: Callable[[], None]
    on_utterance: Callable[[Utterance], None]
    on_await_timeout: Callable[[], None]


class ListenerCore:
    """Pure frame processing, no audio device. Fed with 512-sample int16 frames."""

    def __init__(self, vad: SileroVad, wake: VoskWake, events: ListenerEvents, *, sr: int = 16000,
                 vad_threshold: float = 0.5, end_silence_ms: int = 1000, max_utterance_sec: float = 15.0,
                 wake_grace_ms: int = 2000,
                 pre_roll_ms: int = 400) -> None:
        self.vad = vad
        self.wake = wake
        self.ev = events
        self.sr = sr
        self.th = vad_threshold
        self.frame_sec = FRAME / sr
        self.end_frames = max(1, int(end_silence_ms / 1000 / self.frame_sec))
        # After "Джарвис" people often pause before the question: wait this long for it to start.
        self.grace_frames = max(self.end_frames, int(wake_grace_ms / 1000 / self.frame_sec))
        # Voiced frames after the wake point that mean "the question has started" (the name's tail is shorter).
        self.question_frames = max(1, int(0.3 / self.frame_sec))
        self.max_frames = int(max_utterance_sec / self.frame_sec)
        self.hangover_frames = int(1.0 / self.frame_sec)  # keep feeding Vosk 1 s after speech
        self.segment_cap = int(6.0 / self.frame_sec)
        self.pre_roll: collections.deque[np.ndarray] = collections.deque(maxlen=max(1, int(pre_roll_ms / 1000 / self.frame_sec)))

        self.mode = Mode.WAIT
        self.assistant_speaking = False
        self._lock = threading.RLock()
        self._segment: collections.deque[np.ndarray] = collections.deque(maxlen=self.segment_cap)
        self._in_speech = False
        self._silence = 0
        self._capture: list[np.ndarray] = []
        self._capture_voiced = 0
        self._capture_silence = 0
        self._capture_after = 0     # voiced frames since the capture started (wake point)
        self._capture_source = "wake"
        self._capture_barge = False
        self._await_source = "await"
        self._await_deadline = 0.0
        self._await_voiced = 0
        self._dict_frames: list[np.ndarray] = []
        self._dict_until_pause = False
        self._dict_voiced = 0
        self._dict_silence = 0
        self._dict_max = int(180 / self.frame_sec)
        self._dict_pause_frames = int(1.6 / self.frame_sec)
        self._dict_start_timeout = int(8.0 / self.frame_sec)

    # --- control (any thread) ---
    def to_wait(self) -> None:
        with self._lock:
            self.mode = Mode.WAIT
            self._reset_segment()

    def to_await(self, seconds: float, source: str = "await", resume: bool = False) -> None:
        """resume=True: Jarvis is silent and the user may already be speaking again (the request after "Джарвис"
        in the hot window): speech in progress becomes the start of the command instead of being lost."""
        with self._lock:
            if resume and self.mode is Mode.WAIT and self._in_speech and self._segment:
                segment = list(self._segment)
                self._reset_segment()
                self._start_capture(segment, source)
                return
            self.mode = Mode.AWAIT
            self._await_source = source
            self._await_deadline = time.monotonic() + seconds
            self._await_voiced = 0
            if not resume:
                self.pre_roll.clear()  # it may hold Jarvis's own voice from the speakers

    def start_dictation(self, until_pause: bool) -> None:
        """until_pause=False: record until stop_dictation() (hotkey held); True: stop after a pause (voice)."""
        with self._lock:
            self.mode = Mode.DICTATE
            self._dict_frames = list(self.pre_roll) if not until_pause else []
            self._dict_until_pause = until_pause
            self._dict_voiced = 0
            self._dict_silence = 0

    def stop_dictation(self) -> None:
        with self._lock:
            if self.mode is Mode.DICTATE:
                self._finish_dictation()

    def _finish_dictation(self) -> None:
        frames, voiced = self._dict_frames, self._dict_voiced
        self._dict_frames = []
        self.mode = Mode.WAIT
        self._reset_segment()
        audio = np.concatenate(frames).astype(np.float32) / 32768.0 if frames and voiced >= 3 else np.zeros(0, np.float32)
        self.ev.on_utterance(Utterance(audio, "dictation"))

    def _reset_segment(self) -> None:
        self._segment.clear()
        self._in_speech = False
        self._silence = 0
        self.wake.reset()

    def _start_capture(self, initial: list[np.ndarray], source: str, barge_in: bool = False) -> None:
        self.mode = Mode.CAPTURE
        self._capture = list(initial)
        self._capture_barge = barge_in
        self._capture_voiced = 1
        self._capture_silence = 0
        # From a wake word the question still has to start; from AWAIT it is already being spoken.
        self._capture_after = 0 if source == "wake" else self.question_frames
        self._capture_source = source

    # --- processing (listener thread) ---
    def process(self, frame: np.ndarray) -> None:
        prob = self.vad(frame)
        voiced = prob >= self.th
        with self._lock:
            if self.mode is Mode.WAIT:
                self._process_wait(frame, voiced)
            elif self.mode is Mode.CAPTURE:
                self._process_capture(frame, voiced)
            elif self.mode is Mode.AWAIT:
                self._process_await(frame, voiced)
            elif self.mode is Mode.DICTATE:
                self._process_dictate(frame, voiced)
            self.pre_roll.append(frame)

    def _process_wait(self, frame: np.ndarray, voiced: bool) -> None:
        if voiced:
            if not self._in_speech:
                self._segment.extend(self.pre_roll)
                self._in_speech = True
            self._silence = 0
        elif self._in_speech:
            self._silence += 1
            if self._silence > self.hangover_frames:
                self._reset_segment()
                return
        if not self._in_speech:
            return

        self._segment.append(frame)
        pcm = frame.tobytes()
        if self.assistant_speaking:
            word = self.wake.feed_stop(pcm)
            if word:
                log.info("Похоже на стоп-слово: %s", word)
                tail = list(self._segment)[-int(2.5 / self.frame_sec):]
                audio = np.concatenate(tail).astype(np.float32) / 32768.0
                self._reset_segment()
                self.ev.on_stop_word(word, audio)
                return
        if self.wake.feed_wake(pcm):
            log.info("Кодовое слово")
            segment = list(self._segment)
            barge_in = self.assistant_speaking
            if barge_in:
                # Before the name it is Jarvis talking: keep only the name itself (+ recognition lag).
                segment = segment[-int(2.0 / self.frame_sec):]
            self._reset_segment()
            self._start_capture(segment, "wake", barge_in)
            self.ev.on_wake()

    def _process_capture(self, frame: np.ndarray, voiced: bool) -> None:
        self._capture.append(frame)
        if voiced:
            self._capture_voiced += 1
            self._capture_after += 1
            self._capture_silence = 0
        else:
            self._capture_silence += 1
        question_started = self._capture_after >= self.question_frames
        limit = self.end_frames if question_started else self.grace_frames
        if self._capture_source == "hot" and self._capture_voiced * self.frame_sec < 0.9:
            # "Джарвис…" and a pause in the hot window: the request follows the name, wait for it as after a wake.
            limit = self.grace_frames
        if self._capture_silence >= limit or len(self._capture) >= self.max_frames:
            audio = np.concatenate(self._capture).astype(np.float32) / 32768.0
            utt = Utterance(audio, self._capture_source, self._capture_voiced * self.frame_sec, self._capture_barge)
            self._capture = []
            self.mode = Mode.WAIT
            self._reset_segment()
            self.ev.on_utterance(utt)

    def _process_dictate(self, frame: np.ndarray, voiced: bool) -> None:
        self._dict_frames.append(frame)
        if voiced:
            self._dict_voiced += 1
            self._dict_silence = 0
        else:
            self._dict_silence += 1
        n = len(self._dict_frames)
        if n >= self._dict_max:
            self._finish_dictation()
        elif self._dict_until_pause:
            if (self._dict_voiced and self._dict_silence >= self._dict_pause_frames) or                     (not self._dict_voiced and n >= self._dict_start_timeout):
                self._finish_dictation()

    def _process_await(self, frame: np.ndarray, voiced: bool) -> None:
        if voiced:
            self._await_voiced += 1
            if self._await_voiced >= 2:  # ~64 ms of speech, ignores clicks
                self._start_capture(list(self.pre_roll) + [frame], self._await_source)
                self.ev.on_speech_start()
                return
        else:
            self._await_voiced = 0
        if time.monotonic() > self._await_deadline:
            self.mode = Mode.WAIT
            self._reset_segment()
            self.ev.on_await_timeout()


class Microphone:
    """Owns the input stream and a worker thread that runs ListenerCore."""

    def __init__(self, core: ListenerCore, device: str = "", sr: int = 16000) -> None:
        self.core = core
        self.sr = sr
        self.device_spec = device
        self.device = resolve_device(device, "input")
        self.last_frame = time.monotonic()
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=300)
        self._stream: sd.InputStream | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def paused(self) -> bool:
        return self._stream is None

    def _callback(self, indata, _frames, _time, status) -> None:
        if status and status.input_overflow:
            log.debug("input overflow")
        self.last_frame = time.monotonic()
        try:
            self._q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass  # worker is behind; dropping audio is better than growing memory

    def _worker(self) -> None:
        while self._running:
            try:
                frame = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.core.process(frame)
            except Exception:
                log.exception("Ошибка обработки аудио")

    def start(self) -> None:
        self._running = True
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="listener", daemon=True)
            self._thread.start()
        self.resume()

    def resume(self) -> None:
        if self._stream is not None:
            return
        self.core.to_wait()
        GATE.__enter__()  # held while the stream is open: device re-reads wait for it to close
        try:
            self._stream = sd.InputStream(samplerate=self.sr, channels=1, dtype="int16", blocksize=FRAME,
                                          device=self.device, callback=self._callback)
            self._stream.start()
        except Exception:
            self._stream = None
            GATE.__exit__()
            raise
        self.last_frame = time.monotonic()
        name = sd.query_devices(self._stream.device)["name"]
        log.info("Микрофон: %s", name)

    @property
    def silent_for(self) -> float:
        """Seconds since the last audio block: a removed USB microphone just stops calling back."""
        return time.monotonic() - self.last_frame if self._stream is not None else 0.0

    def reopen(self, refresh: Callable[[], bool]) -> None:
        """Closes the stream, lets PortAudio re-read devices (`refresh`) and opens the current microphone."""
        was_open = self._stream is not None
        self.pause()
        refresh()
        self.device = resolve_device(self.device_spec, "input")
        if was_open:
            self.resume()

    def pause(self) -> None:
        """Closes the stream so Windows shows the microphone as not in use."""
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except sd.PortAudioError as exc:
            log.warning("Микрофон закрылся с ошибкой: %s", exc)
        self._stream = None
        GATE.__exit__()
        with self._q.mutex:
            self._q.queue.clear()
        self.core.mode = Mode.PAUSED

    def stop(self) -> None:
        self._running = False
        self.pause()
