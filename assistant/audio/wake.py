"""Wake word and stop words via Vosk restricted to a tiny grammar.

Vosk only sees audio while the VAD reports speech, so idle CPU stays near zero.
The same approach is used by Priler's Jarvis (Rust).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import vosk

log = logging.getLogger("wake")
vosk.SetLogLevel(-1)


class _GrammarSpotter:
    def __init__(self, model: vosk.Model, targets: list[str], decoys: list[str], sr: int) -> None:
        self.targets = [t.lower() for t in targets]
        grammar = list(dict.fromkeys(self.targets + [d.lower() for d in decoys] + ["[unk]"]))
        self._rec = vosk.KaldiRecognizer(model, sr, json.dumps(grammar, ensure_ascii=False))
        self._rec.SetWords(False)

    def feed(self, pcm: bytes) -> str | None:
        if self._rec.AcceptWaveform(pcm):
            text = json.loads(self._rec.Result()).get("text", "")
        else:
            text = json.loads(self._rec.PartialResult()).get("partial", "")
        for word in text.split():
            if word in self.targets:
                self._rec.Reset()
                return word
        return None

    def reset(self) -> None:
        self._rec.Reset()


class VoskWake:
    def __init__(self, model_dir: Path, phrases: list[str], decoys: list[str], stop_words: list[str], sr: int = 16000) -> None:
        if not model_dir.exists():
            raise FileNotFoundError(f"Нет модели Vosk: {model_dir}")
        self._model = vosk.Model(str(model_dir))
        self._wake = _GrammarSpotter(self._model, phrases, decoys, sr)
        self._stop = _GrammarSpotter(self._model, stop_words, [], sr)
        log.info("Wake word: %s", ", ".join(phrases))

    def feed_wake(self, pcm: bytes) -> bool:
        return self._wake.feed(pcm) is not None

    def feed_stop(self, pcm: bytes) -> str | None:
        """Returns the stop word heard, if any."""
        return self._stop.feed(pcm)

    def reset(self) -> None:
        self._wake.reset()
        self._stop.reset()
