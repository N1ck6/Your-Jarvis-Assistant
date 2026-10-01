"""Speaker echo: Jarvis must not stop or wake himself with his own voice from the speakers."""
import asyncio
import types

import miniaudio
import numpy as np
import pytest

from assistant.assistant import Assistant
from assistant.config import load_settings
from assistant.voicepack import PACKS_DIR

GREETING = PACKS_DIR / "jarvis-remaster" / "ru" / "greet_morning.mp3"
pytestmark = pytest.mark.skipif(not GREETING.exists(), reason="voice packs not downloaded")


@pytest.fixture(scope="module")
def stt():
    from assistant.stt import create_stt

    return create_stt("gigaam", "gigaam-v3-e2e-rnnt", "int8")


def fake_assistant(stt, speaking_text):
    calls = []
    me = types.SimpleNamespace(
        cfg=load_settings(), stt=stt, listener=None,
        speaker=types.SimpleNamespace(speaking=True, recent_text=lambda: speaking_text),
        interrupt=lambda: calls.append("interrupt"), _earcon=lambda clip: None,
        _set_state=lambda *a, **k: calls.append("listening"),
    )
    return me, calls


def load(path):
    d = miniaudio.decode_file(str(path), output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=16000)
    return np.frombuffer(d.samples, dtype=np.float32).copy()


def test_own_greeting_is_not_a_stop_word(stt):
    me, calls = fake_assistant(stt, "")  # a recorded clip: its words are unknown to the speaker
    asyncio.run(Assistant._verify_stop_word(me, "хватит", load(GREETING)))
    assert calls == []


def test_real_stop_word_interrupts(stt):
    from assistant.tts.manager import TtsManager

    clip = TtsManager("silero:eugene").synth("Стоп!")
    me, calls = fake_assistant(stt, "Сейчас в Москве плюс девять градусов.")
    asyncio.run(Assistant._verify_stop_word(me, "стоп", clip.samples[::3]))
    assert calls == ["interrupt", "listening"]


def test_stop_word_inside_own_sentence_is_ignored(stt):
    from assistant.tts.manager import TtsManager

    text = "Хватит одного клика, чтобы открыть настройки."
    clip = TtsManager("silero:eugene").synth(text)
    me, calls = fake_assistant(stt, text)
    asyncio.run(Assistant._verify_stop_word(me, "хватит", clip.samples[::3]))
    assert calls == []
