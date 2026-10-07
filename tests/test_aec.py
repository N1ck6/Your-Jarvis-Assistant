"""Echo cancellation and talking over Jarvis: the microphone stops hearing him, his own words never wake him,
a real "Джарвис" over his speech does, and his voice goes down while the user talks."""
import numpy as np
import pytest

pytest.importorskip("livekit")

from assistant.audio import aec as aec_mod  # noqa: E402
from assistant.audio.aec import CHUNK, EchoCanceller, Reference  # noqa: E402
from assistant.audio.listener import ListenerCore, ListenerEvents, Mode  # noqa: E402
from assistant.audio.vad import FRAME, SileroVad  # noqa: E402
from assistant.audio.wake import VoskWake  # noqa: E402
from assistant.paths import MODELS_DIR  # noqa: E402

VOSK = MODELS_DIR / "vosk-model-small-ru-0.22"


@pytest.fixture
def sim_clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(aec_mod, "clock", lambda: now["t"])
    return now


def room(x, rng, delay_ms=60):
    ir = np.zeros(4000)
    d = int(delay_ms * 16)
    ir[d] = 0.8
    ir[d:] += rng.standard_normal(len(ir) - d) * np.exp(-np.arange(len(ir) - d) / 600) * 0.08
    y = np.convolve(x, ir)[: len(x)]
    return np.tanh(1.6 * y) / np.tanh(1.6)


def db(a):
    return 10 * np.log10(np.mean(np.asarray(a, np.float64) ** 2) + 1e-12)


def test_reference_streams_are_summed_on_one_clock(sim_clock):
    ref = Reference()
    a, b = ref.stream(), ref.stream()
    a.write(np.full(CHUNK * 3, 0.1, np.float32), 16000)
    b.write(np.full(CHUNK * 2, 0.2, np.float32), 16000)       # a cue over the speech, the same moment
    start = int(sim_clock["t"] * 100)
    assert np.allclose(ref.take(start), 0.3) and np.allclose(ref.take(start + 1), 0.3)
    assert np.allclose(ref.take(start + 2), 0.1)
    assert not ref.take(start + 3).any()                          # nothing played: silence


def test_echo_is_removed(sim_clock):
    rng = np.random.default_rng(0)
    t = np.arange(16000 * 6) / 16000
    play = (0.3 * np.sin(2 * np.pi * 220 * t) * (1 + np.sin(2 * np.pi * 3 * t)) / 2
            + 0.05 * rng.standard_normal(len(t))).astype(np.float32)   # speech-like: varying, broadband
    mic = room(play, rng) + rng.standard_normal(len(play)) * 0.002
    ref = Reference()
    stream = ref.stream()
    canceller = EchoCanceller(ref)
    out = []
    for i in range(0, len(mic) - FRAME + 1, FRAME):
        sim_clock["t"] = 1000.0 + i / 16000
        stream.write(play[i:i + FRAME], 16000)
        out.append(canceller.process((np.clip(mic[i:i + FRAME], -1, 1) * 32767).astype(np.int16), sim_clock["t"]))
    out = np.concatenate(out).astype(np.float32) / 32767
    half = len(out) // 2
    assert db(mic[half:len(out)]) - db(out[half:]) > 25     # > 25 dB less echo once adapted


def test_passes_the_user_when_jarvis_is_silent(sim_clock):
    voice = (0.2 * np.sin(2 * np.pi * 300 * np.arange(16000 * 2) / 16000)).astype(np.float32)
    canceller = EchoCanceller(Reference())
    out = np.concatenate([canceller.process((voice[i:i + FRAME] * 32767).astype(np.int16), 1000 + i / 16000)
                          for i in range(0, len(voice) - FRAME + 1, FRAME)]).astype(np.float32) / 32767
    assert abs(db(out[8000:]) - db(voice[8000:len(out)])) < 3


class FakeWake:
    """Raw signal "hears" the name on every frame; the cleaned signal never does."""

    def __init__(self):
        self.raw_resets = 0

    def feed_wake_raw(self, pcm):
        return True

    def feed_wake(self, pcm):
        return False

    def feed_stop(self, pcm):
        return None

    def reset(self):
        pass

    def reset_raw(self):
        self.raw_resets += 1


def core_with(events):
    ev = ListenerEvents(on_wake=lambda: events.append("wake"), on_stop_word=lambda w, a: None,
                        on_speech_start=lambda: None, on_utterance=lambda u: events.append(("utt", u)),
                        on_await_timeout=lambda: None, on_talk_over=lambda on: events.append(("talk", on)))
    return ListenerCore(SileroVad(), FakeWake(), ev)


def test_raw_wake_needs_a_voice_in_the_cleaned_signal():
    events = []
    core = core_with(events)
    core.assistant_speaking = True
    silence = np.zeros(FRAME, np.int16)
    loud = (np.sin(np.arange(FRAME) / 3) * 8000).astype(np.int16)
    for _ in range(20):                  # Jarvis's own words: the raw spotter fires, the cleaned signal is empty
        core.process(silence, raw=loud)
    assert "wake" not in events
    for _ in range(5):                   # the user speaks: something is left after cancellation
        core.process(loud, raw=loud)
    assert "wake" in events and core.mode is Mode.CAPTURE


def test_talk_over_turns_jarvis_down_and_back_up():
    events = []
    core = core_with(events)
    core.wake.feed_wake_raw = lambda pcm: False
    core.assistant_speaking = True
    voice = (np.sin(np.arange(FRAME) / 3) * 8000).astype(np.int16)
    for _ in range(6):
        core.process(voice, raw=voice)
    assert ("talk", True) in events
    for _ in range(60):                  # 1.9 s of quiet
        core.process(np.zeros(FRAME, np.int16), raw=voice)
    assert events[-1] == ("talk", False)


@pytest.mark.skipif(not VOSK.exists(), reason="Vosk model missing")
def test_wake_word_still_heard_through_the_canceller(sim_clock):
    from assistant.tts.manager import TtsManager

    clip = TtsManager("silero:eugene").synth("Джарвис, какая погода будет завтра в Казани?")
    pcm = (np.clip(clip.samples[::3], -1, 1) * 32767).astype(np.int16)
    pcm = np.concatenate([np.zeros(16000, np.int16), pcm, np.zeros(32000, np.int16)])
    events = []
    ev = ListenerEvents(on_wake=lambda: events.append("wake"), on_stop_word=lambda w, a: None,
                        on_speech_start=lambda: None, on_utterance=lambda u: events.append(("utt", u)),
                        on_await_timeout=lambda: None)
    core = ListenerCore(SileroVad(), VoskWake(VOSK, ["джарвис"], [], ["стоп"]), ev)
    canceller = EchoCanceller(Reference())
    for i in range(0, len(pcm) - FRAME + 1, FRAME):
        frame = pcm[i:i + FRAME]
        core.process(canceller.process(frame, 1000 + i / 16000), raw=frame)
    utts = [e for e in events if isinstance(e, tuple)]
    assert "wake" in events and utts and len(utts[0][1].audio) / 16000 > 2.0
