"""TTS engines. Each engine synthesizes a whole sentence into an AudioClip."""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod

import numpy as np

from assistant.audio.player import AudioClip
from assistant.downloads import fetch
from assistant.paths import MODELS_DIR
from assistant.tts.text_norm import normalize_ru

log = logging.getLogger("tts")


class TtsEngine(ABC):
    name: str = ""
    online: bool = False

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def prepare_text(self, text: str) -> str:
        return normalize_ru(text)

    @abstractmethod
    def load(self, voice: str) -> None:
        """Download/load everything needed for the voice (may be slow)."""

    @abstractmethod
    def _synth(self, text: str, voice: str, rate: float) -> AudioClip: ...

    def synth(self, text: str, voice: str, rate: float = 1.0) -> AudioClip:
        prepared = self.prepare_text(text)
        if not prepared.strip(" .,!?"):
            return AudioClip(np.zeros(0, dtype=np.float32), 24000)
        with self._lock:
            self.load(voice)
            return self._synth(prepared, voice, rate)


class SileroEngine(TtsEngine):
    """Silero TTS v5.5 (CC BY-NC-SA). Fast on CPU, automatic stress and ё."""

    name = "silero"
    URL = "https://models.silero.ai/models/tts/ru/v5_5_ru.pt"
    SR = 48000

    def __init__(self) -> None:
        super().__init__()
        self._model = None

    def load(self, voice: str) -> None:
        if self._model is not None:
            return
        import torch

        torch.set_num_threads(4)
        path = fetch(self.URL, MODELS_DIR / "silero" / "v5_5_ru.pt", what="Silero TTS v5.5 (145 МБ)")
        t = time.perf_counter()
        importer = torch.package.PackageImporter(str(path))
        self._model = importer.load_pickle("tts_models", "model")
        log.info("Silero загружен за %.1f с", time.perf_counter() - t)

    def _synth(self, text: str, voice: str, rate: float) -> AudioClip:
        import torch

        with torch.inference_mode():
            if abs(rate - 1.0) > 0.01:
                pct = int(round(rate * 100))
                ssml = f'<speak><prosody rate="{pct}%">{text}</prosody></speak>'
                audio = self._model.apply_tts(ssml_text=ssml, speaker=voice, sample_rate=self.SR)
            else:
                audio = self._model.apply_tts(text=text, speaker=voice, sample_rate=self.SR)
        return AudioClip(audio.numpy().astype(np.float32), self.SR)


ENGINES: dict[str, type[TtsEngine]] = {"silero": SileroEngine}
