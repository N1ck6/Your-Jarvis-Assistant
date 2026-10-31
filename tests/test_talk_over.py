"""Talking over Jarvis: only a voice turns him down (not the echo loud speakers leave), a conversation in the room
is let go after 2 s, and the volume moves smoothly; in the "pause" mode he stops between words and goes on."""
import numpy as np
import pytest

from assistant.assistant import TALK_OVER_SOFT, Assistant
from assistant.audio.listener import FRAME, ListenerCore, ListenerEvents
from assistant.audio.player import Player, _Speech
from assistant.audio.vad import SileroVad
from assistant.config import load_settings


class FakeWake:
    def feed_wake_raw(self, pcm):
        return False

    def feed_wake(self, pcm):
        return False

    def feed_stop(self, pcm):
        return None

    def reset(self):
        pass

    def reset_raw(self):
        pass


def core_with(events):
    ev = ListenerEvents(on_wake=lambda: None, on_stop_word=lambda w, a: None, on_speech_start=lambda: None,
                        on_utterance=lambda u: None, on_await_timeout=lambda: None,
                        on_talk_over=lambda on: events.append(on))
    core = ListenerCore(SileroVad(), FakeWake(), ev)
    core.assistant_speaking = True
    return core


@pytest.fixture(scope="module")
def voices():
    from assistant.tts.manager import TtsManager

    tts = TtsManager("silero:eugene")
    jarvis = tts.synth("Завтра в Казани облачно, около пяти градусов, к вечеру возможен небольшой дождь. "
                       "Ветер северо-западный, до семи метров в секунду, так что лучше взять зонт.")
    user = tts.synth("Слушай, а это он про субботу говорит или про воскресенье, я не расслышал.")
    as16k = lambda clip: np.clip(clip.samples[::3], -1, 1).astype(np.float32) * 12000
    return as16k(jarvis), as16k(user)


def feed(core, cleaned, raw):
    for i in range(0, min(len(cleaned), len(raw)) - FRAME + 1, FRAME):
        core.process(cleaned[i:i + FRAME].astype(np.int16), raw=raw[i:i + FRAME].astype(np.int16))


def test_echo_from_loud_speakers_is_not_a_voice(voices):
    """Distorted speakers: a third of Jarvis's voice survives the canceller and sounds like speech."""
    echo, _ = voices
    events = []
    core = core_with(events)
    feed(core, 0.3 * echo, echo)
    assert events == []


def test_a_voice_over_the_echo_still_turns_him_down(voices):
    echo, user = voices
    events = []
    core = core_with(events)
    feed(core, 0.1 * echo[:16000 * 3], echo[:16000 * 3])        # the canceller's usual leak, learned
    assert events == []
    n = min(len(user), len(echo) - 16000 * 3)
    rest = echo[16000 * 3:16000 * 3 + n]
    feed(core, 0.1 * rest + user[:n], rest + user[:n])
    assert events and events[0] is True


def test_a_conversation_is_let_go_after_two_seconds(voices):
    _, user = voices
    events = []
    core = core_with(events)
    talk = np.concatenate([user, user])
    feed(core, talk, talk)
    assert events == [True, False]                  # down, then back up although the talk goes on
    core.assistant_speaking = False                 # the next answer listens again
    core.process(np.zeros(FRAME, np.int16), raw=np.zeros(FRAME, np.int16))
    core.assistant_speaking = True
    feed(core, user, user)
    assert events[2] is True


def tone(sec, sr=24000):
    return (0.5 * np.sin(2 * np.pi * 220 * np.arange(int(sec * sr)) / sr)).astype(np.float32)


def test_the_volume_moves_smoothly():
    player = Player.__new__(Player)
    player.duck, player.gain, player.hold = 1.0, 1.0, False
    speech = _Speech(player, tone(1.0), 24000)
    speech.read(480)
    player.duck = 0.3
    out, _ = speech.read(480)                       # 20 ms later: on the way down, not there yet
    assert 0.3 < player.gain < 1.0 and np.max(np.abs(np.diff(np.abs(out)))) < 0.1
    for _ in range(10):
        speech.read(480)
    assert player.gain == pytest.approx(0.3)


def test_pause_stops_between_words_and_goes_on():
    sr = 24000
    player = Player.__new__(Player)
    player.duck, player.gain, player.hold = 1.0, 1.0, False
    data = np.concatenate([tone(0.3), np.zeros(int(0.1 * sr), np.float32), tone(0.5)])
    speech = _Speech(player, data, sr)
    speech.read(2400)                               # 0.1 s into the first word
    player.hold = True
    played = []
    for _ in range(30):
        out, over = speech.read(480)
        played.append(out)
    assert speech.paused and int(0.3 * sr) <= speech.pos <= int(0.4 * sr)   # stopped in the gap
    assert not np.any(played[-1])
    player.hold = False
    rest = []
    over = False
    while not over:
        out, over = speech.read(480)
        rest.append(out)
    assert speech.pos == len(data) and np.any(np.concatenate(rest))


def make_app(mode):
    app = Assistant.__new__(Assistant)
    app.cfg = load_settings()
    app.cfg.audio.talk_over = mode
    app._held = None
    app.player = Player.__new__(Player)
    app.player.duck, app.player.gain, app.player.hold = 1.0, 1.0, False
    app.mic = type("Mic", (), {"aec": type("Aec", (), {"adapted": True})()})()
    return app


@pytest.mark.parametrize("mode, duck, hold", [("soft", TALK_OVER_SOFT, False), ("strong", 0.3, False),
                                              ("pause", 1.0, True), ("off", 1.0, False)])
def test_the_setting_picks_what_talking_over_does(mode, duck, hold):
    app = make_app(mode)
    app.cfg.audio.talk_over_volume = 0.3
    app._on_talk_over(True)
    assert app.player.duck == duck and app.player.hold is hold
    app._on_talk_over(False)
    assert app.player.duck == 1.0 and app.player.hold is False
