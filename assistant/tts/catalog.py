"""Synthesized voice(s). Voice id format: "<engine>:<voice>".

Only one synthesized voice is used: Silero «Евгений». Recorded Jarvis reactions live in assistant/voicepack.py.
New engines (e.g. a cloned XTTS voice) are added here and in tts/engines.py.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class VoiceSpec:
    id: str
    title: str
    engine: str
    voice: str
    gender: str
    online: bool
    size_mb: int
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


VOICES: list[VoiceSpec] = [
    VoiceSpec("silero:eugene", "Silero · Евгений", "silero", "eugene", "male", False, 145,
              ["Полностью офлайн", "Естественные ударения и «ё»", "~0,1 с на фразу на CPU", "Спокойный низкий голос"],
              ["Лицензия CC BY-NC-SA (только некоммерческое использование)"]),
]

BY_ID = {v.id: v for v in VOICES}


def get_voice(voice_id: str) -> VoiceSpec:
    if voice_id not in BY_ID:
        raise KeyError(f"Неизвестный голос: {voice_id}")
    return BY_ID[voice_id]
