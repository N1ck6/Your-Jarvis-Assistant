"""TTS engines. Each engine synthesizes a whole sentence into an AudioClip."""
from __future__ import annotations

import logging
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict

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


class _Stubs:
    """F5-TTS's inference module imports its trainer (wandb, datasets), an ASR pipeline (transformers, sklearn),
    plotting and pydub at the top: ~8 s and hundreds of MB that synthesis never uses. They are replaced by empty
    modules for the duration of the import only."""

    NAMES = {"f5_tts.model.trainer": {"Trainer": object}, "transformers": {"pipeline": None},
             "matplotlib": {"use": lambda *a, **k: None}, "matplotlib.pylab": {},
             "pydub": {"AudioSegment": None, "silence": None}}

    def __enter__(self) -> None:
        import types

        self.added = [n for n in self.NAMES if n not in sys.modules]
        for name in self.added:
            module = types.ModuleType(name)
            module.__dict__.update(self.NAMES[name])
            sys.modules[name] = module
        if "matplotlib" in self.added:
            sys.modules["matplotlib"].pylab = sys.modules["matplotlib.pylab"]  # type: ignore[attr-defined]

    def __exit__(self, *_exc) -> None:
        for name in self.added:
            sys.modules.pop(name, None)  # a real import elsewhere later gets the real module


class CloneEngine(TtsEngine):
    """Jarvis's own voice: ESpeech-TTS-1 (F5-TTS trained on Russian, Apache-2.0) cloning a recorded pack.

    Voice id = pack id ("jarvis-remaster"). Needs an NVIDIA GPU: ~0.3 s of synthesis per second of speech,
    plus ~0.7 s per sentence for the reference. Stress marks come from Silero Stress (MIT, ~250 MB of RAM;
    RUAccent needed 2.7 GB). Short frequent phrases are kept synthesized in memory.
    """

    name = "clone"
    REPO = "ESpeech/ESpeech-TTS-1_RL-V2"
    CKPT = "espeech_tts_rlv2.pt"
    SR = 24000
    nfe_step = 16  # set from [tts] clone_nfe: fewer steps = faster, more = cleaner

    CACHE_SIZE = 96          # short phrases ("Сейчас покажу.", "Готово.") are not synthesized twice
    CACHE_MAX_CHARS = 80

    def __init__(self) -> None:
        super().__init__()
        self._model = None
        self._vocoder = None
        self._accent = None
        self._refs: dict[str, tuple] = {}
        self._cache: OrderedDict[tuple, AudioClip] = OrderedDict()

    def load(self, voice: str) -> None:
        if self._model is None:
            import torch

            if not torch.cuda.is_available():
                raise RuntimeError("для голоса Джарвиса нужна видеокарта NVIDIA и PyTorch с CUDA")
            t = time.perf_counter()
            with _Stubs():
                from f5_tts.infer.utils_infer import load_model, load_vocoder
                from f5_tts.model.backbones.dit import DiT
            from huggingface_hub import hf_hub_download
            from silero_stress import load_accentor

            cfg = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
            self._model = load_model(DiT, cfg, hf_hub_download(self.REPO, self.CKPT),
                                     vocab_file=hf_hub_download(self.REPO, "vocab.txt"), device="cuda")
            self._vocoder = load_vocoder(device="cuda")
            self._accent = load_accentor()
            log.info("Клон-голос загружен за %.1f с", time.perf_counter() - t)
        if voice not in self._refs:
            import torch

            from assistant.voicepack import clone_reference

            audio, text = clone_reference(voice, self.SR)
            self._refs[voice] = ((torch.from_numpy(audio).unsqueeze(0), self.SR), self._accent(text) + " ")
            self._synth(self._stress("Готово."), voice, 1.0)  # the first run compiles CUDA kernels: not on the user

    def synth(self, text: str, voice: str, rate: float = 1.0) -> AudioClip:
        key = (voice, round(rate, 2), self.nfe_step, text)
        if len(text) <= self.CACHE_MAX_CHARS and key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        clip = super().synth(text, voice, rate)
        if len(text) <= self.CACHE_MAX_CHARS and clip.samples.size:
            self._cache[key] = clip
            while len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)
        return clip

    def _stress(self, text: str) -> str:
        """Stress marks for the model; English words already carry theirs from the CMU dictionary
        (the accentor would move them), so those words are kept as they are."""
        if "+" not in text:
            return self._accent(text)
        words = text.split(" ")
        out = self._accent(text.replace("+", "")).split(" ")
        if len(out) != len(words):
            return " ".join(out)
        return " ".join(w if "+" in w else o for w, o in zip(words, out))

    def _synth(self, text: str, voice: str, rate: float) -> AudioClip:
        import torch
        from f5_tts.infer.utils_infer import infer_batch_process

        ref, ref_text = self._refs[voice]
        gen = self._stress(text)
        with torch.inference_mode():
            wav, sr, _ = next(infer_batch_process(ref, ref_text, [gen], self._model, self._vocoder,
                                                  nfe_step=self.nfe_step, speed=rate, progress=None, device="cuda"))
        return AudioClip(np.asarray(wav, dtype=np.float32), sr)


class PiperEngine(TtsEngine):
    """Piper (VITS, ONNX): a ~60 MB voice on the CPU, ~0.1 s a phrase. The light Jarvis voice is the clone distilled
    into Piper (scripts/make_voice_dataset.py + scripts/piper/train.ps1). Voice id = model file name in models/piper.
    The text gets the same normalization as the clone was trained on; stress comes from espeak-ng."""

    name = "piper"

    def __init__(self) -> None:
        super().__init__()
        self._voices: dict[str, object] = {}

    @staticmethod
    def model_path(voice: str):
        return MODELS_DIR / "piper" / f"{voice}.onnx"

    def load(self, voice: str) -> None:
        if voice in self._voices:
            return
        from piper import PiperVoice

        path = self.model_path(voice)
        if not path.exists():
            raise FileNotFoundError(f"нет модели {path}")
        t = time.perf_counter()
        self._voices[voice] = PiperVoice.load(path)
        log.info("Piper %s загружен за %.1f с", voice, time.perf_counter() - t)

    def _synth(self, text: str, voice: str, rate: float) -> AudioClip:
        from piper import SynthesisConfig

        model = self._voices[voice]
        chunks = list(model.synthesize(text, SynthesisConfig(length_scale=1.0 / max(rate, 0.5))))  # type: ignore[attr-defined]
        if not chunks:
            return AudioClip(np.zeros(0, dtype=np.float32), 22050)
        return AudioClip(np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32), chunks[0].sample_rate)


ENGINES: dict[str, type[TtsEngine]] = {"silero": SileroEngine, "clone": CloneEngine, "piper": PiperEngine}
