"""Current voice + engine cache. Voice can be switched live from the voice lab."""
from __future__ import annotations

import logging
import threading
import time

from assistant.audio.player import AudioClip
from assistant.tts.catalog import VoiceSpec, get_voice
from assistant.tts.engines import ENGINES, TtsEngine

log = logging.getLogger("tts")


class TtsManager:
    def __init__(self, voice_id: str, rate: float = 1.0, fallback_id: str = "silero:eugene") -> None:
        self._engines: dict[str, TtsEngine] = {}
        self._lock = threading.Lock()
        self.rate = rate
        self.fallback_id = fallback_id
        try:
            self.voice: VoiceSpec = get_voice(voice_id)
        except KeyError:  # a removed voice left in data/settings.json
            log.warning("Голос %s больше не поддерживается, использую %s", voice_id, fallback_id)
            self.voice = get_voice(fallback_id)

    def engine(self, name: str) -> TtsEngine:
        with self._lock:
            if name not in self._engines:
                self._engines[name] = ENGINES[name]()
            return self._engines[name]

    def set_voice(self, voice_id: str) -> VoiceSpec:
        spec = get_voice(voice_id)
        self.preload(spec)
        self.voice = spec
        log.info("Голос: %s", spec.title)
        return spec

    def preload(self, spec: VoiceSpec | None = None) -> None:
        spec = spec or self.voice
        self.engine(spec.engine).load(spec.voice)

    def synth_with(self, spec: VoiceSpec, text: str, rate: float | None = None) -> AudioClip:
        return self.engine(spec.engine).synth(text, spec.voice, self.rate if rate is None else rate)

    def synth(self, text: str) -> AudioClip:
        spec = self.voice
        t = time.perf_counter()
        try:
            clip = self.synth_with(spec, text)
        except Exception as exc:  # online voice without network, broken model...
            if spec.id == self.fallback_id:
                raise
            log.warning("Голос %s недоступен (%s), использую %s", spec.id, exc, self.fallback_id)
            clip = self.synth_with(get_voice(self.fallback_id), text)
        log.debug("TTS %.0f мс: %s", (time.perf_counter() - t) * 1000, text[:60])
        return clip
