"""Dictation flow in the assistant: start cue before recording, phrases typed in order while speaking,
hotkey tap vs hold, nothing typed while the hotkey's Ctrl/Alt are still held."""
import asyncio
import time

import numpy as np

from assistant.assistant import Assistant
from assistant.audio import earcons
from assistant.audio.listener import Utterance
from assistant.config import load_settings
from assistant.core import ConsoleUi
from assistant.stt import SttResult


class FakeListener:
    def __init__(self):
        self.dictating = False
        self.started = 0
        self.hands_free = None

    def start_dictation(self, until_pause, pause_sec=3.0):
        self.dictating, self.started = True, self.started + 1

    def stop_dictation(self):
        self.dictating = False
        self.app._on_utterance(Utterance(np.zeros(0, np.float32), "dictation"))

    def end_dictation_after_pause(self, seconds):
        self.hands_free = seconds


class FakeStt:
    def __init__(self, texts):
        self.texts = list(texts)

    def recognize(self, audio):
        time.sleep(0.02 * len(self.texts))  # earlier phrases take longer: order must still hold
        return SttResult(self.texts.pop(0), logprobs=[-0.05])


def make_app(texts=()):
    app = Assistant.__new__(Assistant)
    app.cfg = load_settings()
    app.ui = ConsoleUi()
    app.state = None
    app.loop = None
    app._task = None
    app.interruptions = 0
    app.listener = FakeListener()
    app.listener.app = app
    app.mic = type("Mic", (), {"paused": False, "resume": lambda self: None, "pause": lambda self: None})()
    app.music = type("Music", (), {"duck": lambda self, on: None})()
    app.speaker = type("Speaker", (), {"stop": lambda self: None})()
    app.cues = []
    app.player = type("Player", (), {"effect": lambda self, clip, vol: app.cues.append(clip)})()
    app.stt = FakeStt(texts)
    app._dictation_paste = None
    app._muted_before_dictation = False
    app._dictating = False
    app._dict_chain = None
    app._dict_pasted = 0
    app._dict_key_stops = False
    app.keys_held = lambda: False
    return app


def part(sec=1.5, source="dictation_part"):
    return Utterance(np.ones(int(16000 * sec), np.float32) * 0.1, source, sec)


async def drain(app):
    for _ in range(100):
        if app._dict_chain is None:
            return
        await asyncio.sleep(0.02)


def test_cue_plays_before_recording_starts():
    async def run():
        app = make_app()
        assert app.start_dictation(until_pause=False, paste=lambda t: None)
        assert app.cues == [earcons.DICTATE] and app.listener.started == 0   # the cue first
        await asyncio.sleep(earcons.DICTATE.seconds + 0.1)
        assert app.listener.started == 1
    asyncio.run(run())


def test_phrases_are_typed_in_order_as_they_come():
    async def run():
        app = make_app(["Первая фраза.", "Вторая фраза.", "И третья."])
        pasted = []
        app.start_dictation(until_pause=False, paste=pasted.append)
        await asyncio.sleep(earcons.DICTATE.seconds + 0.1)
        app._on_utterance(part())
        app._on_utterance(part())
        await asyncio.sleep(0.3)
        assert pasted == ["Первая фраза.", " Вторая фраза."]        # typed before the dictation ended
        app._on_utterance(part(source="dictation"))
        await drain(app)
        assert pasted == ["Первая фраза.", " Вторая фраза.", " И третья."] and not app._dictating
    asyncio.run(run())


def test_nothing_is_typed_while_the_hotkey_is_held():
    async def run():
        app = make_app(["Фраза."])
        held = {"on": True}
        app.keys_held = lambda: held["on"]
        pasted = []
        app.start_dictation(until_pause=False, paste=pasted.append)
        app._on_utterance(part())
        await asyncio.sleep(0.3)
        assert pasted == []
        held["on"] = False
        await asyncio.sleep(0.2)
        assert pasted == ["Фраза."]
    asyncio.run(run())


def test_tap_goes_hands_free_and_second_tap_stops():
    async def run():
        app = make_app()
        app.dictation_key_down(lambda t: None)
        await asyncio.sleep(earcons.DICTATE.seconds + 0.1)
        app.dictation_key_up(0.2)
        assert app._dictating and app.listener.hands_free == 30.0
        app.dictation_key_down(lambda t: None)       # second tap
        app.dictation_key_up(0.1)
        await drain(app)
        assert not app._dictating
    asyncio.run(run())


def test_hold_release_stops_and_release_during_cue_records_nothing():
    async def run():
        app = make_app()
        app.dictation_key_down(lambda t: None)
        app.dictation_key_up(0.5)                     # a hold, released while the start cue still plays
        await drain(app)
        await asyncio.sleep(earcons.DICTATE.seconds + 0.1)
        assert not app._dictating and app.listener.started == 0   # released before recording began
    asyncio.run(run())
