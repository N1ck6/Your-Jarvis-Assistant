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


class CloneEngine(TtsEngine):
    """Jarvis's own voice: ESpeech-TTS-1 (F5-TTS trained on Russian, Apache-2.0) cloning a recorded pack.

    Voice id = pack id ("jarvis-og"). Needs an NVIDIA GPU: ~0.3 s of synthesis per second of speech,
    plus ~1 s per sentence for the reference. Stress marks come from RUAccent.
    """

    name = "clone"
    REPO = "ESpeech/ESpeech-TTS-1_RL-V2"
    CKPT = "espeech_tts_rlv2.pt"
    SR = 24000
    nfe_step = 16  # set from [tts] clone_nfe: fewer steps = faster, more = cleaner

    def __init__(self) -> None:
        super().__init__()
        self._model = None
        self._vocoder = None
        self._accent = None
        self._refs: dict[str, tuple] = {}

    def load(self, voice: str) -> None:
        if self._model is None:
            import torch

            if not torch.cuda.is_available():
                raise RuntimeError("для голоса Джарвиса нужна видеокарта NVIDIA и PyTorch с CUDA")
            from f5_tts.infer.utils_infer import load_model, load_vocoder
            from f5_tts.model import DiT
            from huggingface_hub import hf_hub_download
            from ruaccent import RUAccent

            t = time.perf_counter()
            cfg = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
            self._model = load_model(DiT, cfg, hf_hub_download(self.REPO, self.CKPT),
                                     vocab_file=hf_hub_download(self.REPO, "vocab.txt"), device="cuda")
            self._vocoder = load_vocoder(device="cuda")
            self._accent = RUAccent()
            self._accent.load(omograph_model_size="turbo3.1", use_dictionary=True)
            log.info("Клон-голос загружен за %.1f с", time.perf_counter() - t)
        if voice not in self._refs:
            import torch

            from assistant.voicepack import clone_reference

            audio, text = clone_reference(voice, self.SR)
            self._refs[voice] = ((torch.from_numpy(audio).unsqueeze(0), self.SR),
                                 self._accent.process_all(text) + " ")

    def _synth(self, text: str, voice: str, rate: float) -> AudioClip:
        import torch
        from f5_tts.infer.utils_infer import infer_batch_process

        ref, ref_text = self._refs[voice]
        gen = self._accent.process_all(text)
        with torch.inference_mode():
            wav, sr, _ = next(infer_batch_process(ref, ref_text, [gen], self._model, self._vocoder,
                                                  nfe_step=self.nfe_step, speed=rate, progress=None, device="cuda"))
        return AudioClip(np.asarray(wav, dtype=np.float32), sr)


ENGINES: dict[str, type[TtsEngine]] = {"silero": SileroEngine, "clone": CloneEngine}
