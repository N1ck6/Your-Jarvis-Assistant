"""End-to-end check of VAD + wake word + capture on synthesized speech (no microphone)."""
import numpy as np
import pytest

from assistant.audio.listener import ListenerCore, ListenerEvents, Mode
from assistant.audio.vad import FRAME, SileroVad
from assistant.audio.wake import VoskWake
from assistant.paths import MODELS_DIR
from assistant.tts.manager import TtsManager

VOSK = MODELS_DIR / "vosk-model-small-ru-0.22"
pytestmark = pytest.mark.skipif(not VOSK.exists(), reason="Vosk model missing")


@pytest.fixture(scope="module")
def tts():
    return TtsManager("silero:eugene")


def speech_16k(tts, text: str) -> np.ndarray:
    clip = tts.synth(text)
    assert clip.sr == 48000
    audio = clip.samples[::3]
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16)


def run(core: ListenerCore, pcm: np.ndarray) -> None:
    silence = np.zeros(16000, dtype=np.int16)
    pcm = np.concatenate([silence, pcm, silence, silence])
    for i in range(0, len(pcm) - FRAME + 1, FRAME):
        core.process(pcm[i:i + FRAME])


def make_core(events_log):
    ev = ListenerEvents(
        on_wake=lambda: events_log.append("wake"),
        on_stop_word=lambda w: events_log.append(("stop", w)),
        on_speech_start=lambda: events_log.append("speech"),
        on_utterance=lambda u: events_log.append(("utt", u.source, len(u.audio) / 16000)),
        on_await_timeout=lambda: events_log.append("timeout"),
    )
    stops = ["стоп", "хватит", "замолчи", "тихо", "подожди", "погоди", "стой", "секунду", "отмена"]
    return ListenerCore(SileroVad(), VoskWake(VOSK, ["джарвис", "джервис"], ["джон", "давай"], stops), ev)


def test_wake_and_capture(tts):
    log = []
    core = make_core(log)
    run(core, speech_16k(tts, "Джарвис, какая погода будет завтра в Казани?"))
    assert log[0] == "wake"
    utt = [e for e in log if isinstance(e, tuple) and e[0] == "utt"]
    assert len(utt) == 1 and utt[0][1] == "wake"
    assert utt[0][2] > 2.0  # the whole phrase incl. the wake word was captured
    assert core.mode is Mode.WAIT


def test_no_wake_on_other_speech(tts):
    log = []
    core = make_core(log)
    run(core, speech_16k(tts, "Давай посмотрим, что сегодня идёт в кино и какая будет погода."))
    assert "wake" not in log
    assert not [e for e in log if isinstance(e, tuple) and e[0] == "utt"]


def test_stop_word_only_while_speaking(tts):
    log = []
    core = make_core(log)
    pcm = speech_16k(tts, "Стоп.")
    run(core, pcm)
    assert not [e for e in log if e[0] == "stop"]
    core.assistant_speaking = True
    run(core, pcm)
    assert ("stop", "стоп") in log


@pytest.mark.parametrize("phrase,word", [("Подожди.", "подожди"), ("Хватит!", "хватит"), ("Погоди.", "погоди")])
def test_interrupt_words(tts, phrase, word):
    log = []
    core = make_core(log)
    core.assistant_speaking = True
    run(core, speech_16k(tts, phrase))
    assert ("stop", word) in log


def test_dictation_until_pause(tts):
    log = []
    core = make_core(log)
    core.start_dictation(until_pause=True)
    run(core, speech_16k(tts, "Привет, буду через десять минут, возьми хлеба по дороге."))
    utt = [e for e in log if isinstance(e, tuple) and e[0] == "utt"]
    assert utt and utt[0][1] == "dictation" and utt[0][2] > 2.0
    assert core.mode is Mode.WAIT


def test_dictation_hold_ends_on_release(tts):
    log = []
    core = make_core(log)
    core.start_dictation(until_pause=False)
    pcm = speech_16k(tts, "Первая фраза. Вторая фраза после паузы.")
    silence = np.zeros(16000 * 3, dtype=np.int16)  # a long pause does not stop hold-to-dictate
    for chunk in (pcm, silence, pcm):
        for i in range(0, len(chunk) - FRAME + 1, FRAME):
            core.process(chunk[i:i + FRAME])
    assert not log
    core.stop_dictation()
    assert log[0][0] == "utt" and log[0][1] == "dictation" and log[0][2] > 5


def test_await_mode_captures_without_wake(tts):
    log = []
    core = make_core(log)
    core.to_await(10)
    run(core, speech_16k(tts, "Поставь таймер на пять минут."))
    assert "speech" in log
    assert [e for e in log if isinstance(e, tuple) and e[0] == "utt"][0][1] == "await"
