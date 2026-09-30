"""Silero VAD v5 on ONNX Runtime: 512-sample frames at 16 kHz -> speech probability."""
from __future__ import annotations

import numpy as np
import onnxruntime as ort

from assistant.downloads import fetch
from assistant.paths import MODELS_DIR

MODEL_URL = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
FRAME = 512
_CONTEXT = 64


class SileroVad:
    def __init__(self, sample_rate: int = 16000) -> None:
        if sample_rate != 16000:
            raise ValueError("Silero VAD is used at 16 kHz only")
        path = fetch(MODEL_URL, MODELS_DIR / "silero_vad.onnx", what="Silero VAD")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        self._sr = np.array(sample_rate, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _CONTEXT), dtype=np.float32)

    def __call__(self, frame_int16: np.ndarray) -> float:
        x = frame_int16.astype(np.float32).reshape(1, -1) / 32768.0
        if x.shape[1] != FRAME:
            raise ValueError(f"VAD expects {FRAME} samples, got {x.shape[1]}")
        inp = np.concatenate([self._context, x], axis=1)
        out, self._state = self._session.run(None, {"input": inp, "state": self._state, "sr": self._sr})
        self._context = inp[:, -_CONTEXT:]
        return float(out[0][0])
