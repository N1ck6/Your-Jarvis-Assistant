"""Speech-to-text. GigaAM v3 (Sber, MIT) through onnx-asr: Russian, punctuation, ~0.2 s per phrase on CPU."""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger("stt")


class GigaAmStt:
    def __init__(self, model: str = "gigaam-v3-e2e-rnnt", quantization: str = "int8") -> None:
        import onnx_asr

        t = time.perf_counter()
        self._model = onnx_asr.load_model(model, quantization=quantization or None)
        self._lock = threading.Lock()
        log.info("STT %s загружен за %.1f с", model, time.perf_counter() - t)

    def warmup(self) -> None:
        self.transcribe(np.zeros(16000, dtype=np.float32))

    def transcribe(self, audio: np.ndarray, sr: int = 16000) -> str:
        if len(audio) < sr * 0.2:
            return ""
        with self._lock:
            text = self._model.recognize(audio.astype(np.float32), sample_rate=sr)
        return (text or "").strip()


    def transcribe_long(self, audio: np.ndarray, sr: int = 16000, max_sec: float = 20.0) -> str:
        """Dictation can run for minutes; the model is fed pieces cut at the quietest moments."""
        parts = [self.transcribe(chunk, sr) for chunk in split_at_pauses(audio, sr, max_sec)]
        return " ".join(p for p in parts if p).strip()


def split_at_pauses(audio: np.ndarray, sr: int = 16000, max_sec: float = 20.0, min_sec: float = 8.0) -> list[np.ndarray]:
    if len(audio) <= max_sec * sr:
        return [audio]
    hop = int(0.03 * sr)
    n = len(audio) // hop
    energy = np.sqrt(np.mean(audio[:n * hop].reshape(n, hop) ** 2, axis=1))
    chunks, start = [], 0
    while len(audio) - start > max_sec * sr:
        lo, hi = (start + int(min_sec * sr)) // hop, (start + int(max_sec * sr)) // hop
        cut = (lo + int(np.argmin(energy[lo:hi]))) * hop
        chunks.append(audio[start:cut])
        start = cut
    chunks.append(audio[start:])
    return chunks


def create_stt(engine: str, model: str, quantization: str = "int8") -> GigaAmStt:
    if engine == "gigaam":
        return GigaAmStt(model, quantization)
    raise ValueError(f"Неизвестный STT: {engine}")
