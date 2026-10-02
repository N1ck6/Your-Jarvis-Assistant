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
        on_stop_word=lambda w, audio: events_log.append(("stop", w)),
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
    run(core, np.concatenate([speech_16k(tts, "Привет, буду через десять минут, возьми хлеба по дороге."), pause(3.2)]))
    utt = [e for e in log if isinstance(e, tuple) and e[0] == "utt"]
    assert utt and utt[-1][1] == "dictation" and sum(u[2] for u in utt) > 2.0
    assert core.mode is Mode.WAIT


def test_dictation_comes_out_phrase_by_phrase(tts):
    """Each phrase is handed over at the pause after it, while the user goes on: not all at once at the end."""
    log = []
    core = make_core(log)
    core.start_dictation(until_pause=False)
    pcm = trimmed(tts, "Первая фраза диктовки.")
    for chunk in (pcm, pause(1.2), trimmed(tts, "Вторая фраза после паузы."), pause(1.2)):
        for i in range(0, len(chunk) - FRAME + 1, FRAME):
            core.process(chunk[i:i + FRAME])
    parts = [e for e in log if isinstance(e, tuple) and e[0] == "utt"]
    assert [p[1] for p in parts] == ["dictation_part", "dictation_part"]
    assert all(1.0 < p[2] < 3.5 for p in parts), parts   # one phrase each, silence before it trimmed
    assert core.mode is Mode.DICTATE                       # a long pause does not stop hold-to-dictate
    core.stop_dictation()
    assert log[-1][:2] == ("utt", "dictation") and core.mode is Mode.WAIT


def test_dictation_tail_is_the_last_part(tts):
    log = []
    core = make_core(log)
    core.start_dictation(until_pause=False)
    pcm = trimmed(tts, "Фраза без паузы в конце")
    for i in range(0, len(pcm) - FRAME + 1, FRAME):
        core.process(pcm[i:i + FRAME])
    core.stop_dictation()
    utt = [e for e in log if isinstance(e, tuple) and e[0] == "utt"]
    assert len(utt) == 1 and utt[0][1] == "dictation" and utt[0][2] > 1.0


def test_await_mode_captures_without_wake(tts):
    log = []
    core = make_core(log)
    core.to_await(10)
    run(core, speech_16k(tts, "Поставь таймер на пять минут."))
    assert "speech" in log
    assert [e for e in log if isinstance(e, tuple) and e[0] == "utt"][0][1] == "await"


def trimmed(tts, text):
    pcm = speech_16k(tts, text)
    loud = np.nonzero(np.abs(pcm) > 400)[0]
    return pcm[loud[0]:loud[-1] + 1]  # exact pauses: drop the synthesizer's own silence


def pause(sec):
    return np.zeros(int(16000 * sec), dtype=np.int16)


@pytest.mark.parametrize("parts,min_sec", [
    (["Джарвис.", 0.9, "Какая погода будет завтра в Казани?"], 3.0),        # pause after the name
    (["Джарвис.", 1.5, "Поставь таймер на пять минут."], 2.5),
    (["Джарвис, напомни через десять минут", 0.8, "выключить плиту на кухне."], 3.5),  # pause inside the question
])
def test_whole_question_is_captured(tts, parts, min_sec):
    log = []
    core = make_core(log)
    pcm = np.concatenate([pause(p) if isinstance(p, float) else trimmed(tts, p) for p in parts])
    run(core, pcm)
    utt = [e for e in log if isinstance(e, tuple) and e[0] == "utt"]
    assert len(utt) == 1, utt          # one utterance, not cut in two
    assert utt[0][2] >= min_sec


def test_bare_wake_ends_after_grace(tts):
    log = []
    core = make_core(log)
    run(core, np.concatenate([trimmed(tts, "Джарвис."), pause(2.0)]))  # the grace is 2 s
    utt = [e for e in log if isinstance(e, tuple) and e[0] == "utt"]
    assert len(utt) == 1 and utt[0][2] < 4.0


def test_hot_window_name_then_pause_then_request_is_one_utterance(tts):
    """"Джарвис… (pause) …объясни иначе" in the hot window: the name alone must not end the recording."""
    log = []
    core = make_core(log)
    core.to_await(8, "hot")
    pcm = np.concatenate([pause(0.3), trimmed(tts, "Джарвис."), pause(1.4), trimmed(tts, "Объясни это другими словами.")])
    for i in range(0, len(pcm) - FRAME + 1, FRAME):
        core.process(pcm[i:i + FRAME])
    run(core, pause(0.1))
    utt = [e for e in log if isinstance(e, tuple) and e[0] == "utt"]
    assert len(utt) == 1 and utt[0][1] == "hot" and utt[0][2] > 2.5, utt


def test_rearm_keeps_speech_already_started(tts):
    """The window re-opens while the user already speaks again: that speech starts the command."""
    log = []
    core = make_core(log)
    pcm = trimmed(tts, "Поставь таймер на пять минут.")
    half = len(pcm) // 2 // FRAME * FRAME
    for i in range(0, half, FRAME):
        core.process(pcm[i:i + FRAME])     # WAIT mode: VAD is collecting a segment
    core.to_await(8, "await", resume=True)
    assert core.mode is Mode.CAPTURE
    rest = np.concatenate([pcm[half:], pause(1.5)])
    for i in range(0, len(rest) - FRAME + 1, FRAME):
        core.process(rest[i:i + FRAME])
    utt = [e for e in log if isinstance(e, tuple) and e[0] == "utt"]
    assert len(utt) == 1 and utt[0][1] == "await" and utt[0][2] >= len(pcm) / 16000 * 0.9
