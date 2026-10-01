"""Owner's voice profile: Jarvis reacts only to the voice it was taught (speaker verification).

WeSpeaker ResNet34 (VoxCeleb, Apache-2.0, 26 MB ONNX) turns ~1 s of speech into a 256-number voice print in ~30 ms
on the CPU. The profile is the average print of a few enrollment phrases in data/voiceprint.npy; nothing else is
stored, the audio itself is never kept.
"""
from __future__ import annotations

import logging
import threading
from functools import lru_cache

import numpy as np

from assistant.paths import DATA_DIR, MODELS_DIR

log = logging.getLogger("voiceprint")

MODEL_URL = "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34/resolve/main/voxceleb_resnet34.onnx"
MODEL_FILE = MODELS_DIR / "voiceprint" / "voxceleb_resnet34.onnx"
PROFILE = DATA_DIR / "voiceprint.npy"


@lru_cache(maxsize=4)
def _mel_banks(n_fft: int, sr: int, n_mels: int, low: float = 20.0) -> np.ndarray:
    """Kaldi mel filterbank (triangles on the mel scale 1127 * ln(1 + f / 700))."""
    mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)  # noqa: E731
    high = sr / 2
    mel_low, mel_high = mel(low), mel(high)
    delta = (mel_high - mel_low) / (n_mels + 1)
    fft_mel = mel(np.arange(n_fft // 2) * sr / n_fft)
    banks = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for m in range(n_mels):
        left, center, right = mel_low + m * delta, mel_low + (m + 1) * delta, mel_low + (m + 2) * delta
        up = (fft_mel - left) / (center - left)
        down = (right - fft_mel) / (right - center)
        banks[m, :-1] = np.maximum(0.0, np.minimum(up, down))
    return banks


def fbank(audio: np.ndarray, sr: int = 16000, n_mels: int = 80) -> np.ndarray:
    """80-dim log mel filterbank, Kaldi defaults (25 ms / 10 ms, povey window, pre-emphasis 0.97), mean-normalized."""
    wav = audio.astype(np.float64) * 32768.0  # the model was trained on int16-scaled audio
    win, hop, n_fft = int(0.025 * sr), int(0.010 * sr), 512
    if len(wav) < win:
        return np.zeros((0, n_mels), dtype=np.float32)
    n = 1 + (len(wav) - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    frames = wav[idx]
    frames = frames - frames.mean(axis=1, keepdims=True)                      # remove DC
    frames = np.concatenate([frames[:, :1] * (1 - 0.97), frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(win) / (win - 1))) ** 0.85   # povey
    spec = np.abs(np.fft.rfft(frames * window, n=n_fft)) ** 2
    feats = np.log(np.maximum(spec @ _mel_banks(n_fft, sr, n_mels).T, np.finfo(np.float32).eps))
    feats -= feats.mean(axis=0, keepdims=True)
    return feats.astype(np.float32)


class VoicePrint:
    """Lazily loaded model + the saved owner profile."""

    def __init__(self) -> None:
        self._session = None
        self._lock = threading.Lock()
        self.profile: np.ndarray | None = None
        try:
            if PROFILE.exists():
                self.profile = np.load(PROFILE)
        except (OSError, ValueError):
            log.warning("voiceprint.npy повреждён")

    @property
    def enrolled(self) -> bool:
        return self.profile is not None

    def _model(self):
        if self._session is None:
            import onnxruntime as ort

            from assistant.downloads import fetch

            path = fetch(MODEL_URL, MODEL_FILE, what="модель голоса владельца (26 МБ)")
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2
            self._session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        return self._session

    def embed(self, audio: np.ndarray, sr: int = 16000) -> np.ndarray | None:
        """A unit-length voice print of the speech in `audio` (None if it is too short to judge)."""
        feats = fbank(audio, sr)
        if len(feats) < 50:  # < 0.5 s
            return None
        with self._lock:
            emb = self._model().run(None, {"feats": feats[None]})[0][0]
        return emb / (np.linalg.norm(emb) + 1e-9)

    def similarity(self, audio: np.ndarray) -> float | None:
        """Cosine similarity to the owner (≈0.6–0.8 the same person, ≈0–0.3 someone else); None = no verdict."""
        if self.profile is None:
            return None
        emb = self.embed(audio)
        return None if emb is None else float(np.dot(emb, self.profile))

    def enroll(self, clips: list[np.ndarray]) -> int:
        """Averages the prints of the phrases; returns how many phrases were usable."""
        prints = [e for e in (self.embed(c) for c in clips) if e is not None]
        if not prints:
            return 0
        mean = np.mean(prints, axis=0)
        self.profile = mean / (np.linalg.norm(mean) + 1e-9)
        PROFILE.parent.mkdir(parents=True, exist_ok=True)
        np.save(PROFILE, self.profile)
        return len(prints)

    def forget(self) -> None:
        self.profile = None
        PROFILE.unlink(missing_ok=True)
