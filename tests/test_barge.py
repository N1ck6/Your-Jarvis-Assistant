"""A wake word heard while a request is running: the request is cancelled only when the recognizer confirms the name
(a noise or the TV must not eat it); the Jarvis button pressed while he listens switches him off."""
import asyncio

import numpy as np

from assistant.assistant import Assistant
from assistant.audio import earcons
from assistant.audio.listener import Mode, Utterance
from assistant.config import load_settings
from assistant.core import ConsoleUi, State
from assistant.skills.base import Reply
from assistant.stt import SttResult


class FakeListener:
    mode = Mode.WAIT
    talking_over = False

    def __init__(self):
        self.calls = []

    def to_wait(self):
        self.calls.append("wait")

    def to_await(self, seconds, source="await", resume=False):
        self.calls.append(("await", source))


class FakeBrain:
    def __init__(self):
        self.asked = []
        self.extra, self.deferred = [], []

    async def handle(self, text, addressed=True):
        self.asked.append(text)
        return Reply(spoken=True, listen_after=False)

    def expects_answer(self):
        return False

    def surely_web(self, text):
        return False


def make_app(heard):
    app = Assistant.__new__(Assistant)
    app.cfg = load_settings()
    app.cfg.wake.owner_only = False
    app.ui = ConsoleUi()
    app.state = State.IDLE
    app.state_listeners = []
    app.loop = None
    app._task = None
    app._held, app._held_at = None, 0.0
    app._dictating = False
    app._echo_guard, app._await_until = "", 0.0
    app.interruptions = 0
    app.listener = FakeListener()
    app.mic = type("Mic", (), {"paused": False, "aec": None})()
    app.music = type("Music", (), {"duck": lambda self, on: None})()
    app.speaker = type("Speaker", (), {"stop": lambda self: None, "speaking": False, "last_text": "",
                                       "recent_text": lambda self: ""})()
    app.cues = []
    app.player = type("Player", (), {"effect": lambda self, clip, vol: app.cues.append(clip), "duck": 1.0,
                                     "release": lambda self: setattr(self, "duck", 1.0)})()
    app.stt = type("Stt", (), {"recognize": lambda self, audio: SttResult(heard, logprobs=[-0.05])})()
    app.brain = FakeBrain()
    app.pack = None
    return app


def wake_phrase():
    return Utterance(np.zeros(16000, np.float32), "wake", 1.0)


async def slow_request(app, done):
    app._task = asyncio.current_task()
    await asyncio.sleep(0.3)
    await app._wait_hold()
    done.append("answered")


def test_a_false_wake_word_does_not_eat_the_request():
    async def run():
        app = make_app("ну и шум тут")
        done = []
        request = asyncio.create_task(slow_request(app, done))
        await asyncio.sleep(0.05)
        app._on_wake()
        assert not request.cancelled() and app._held is request and app.player.duck < 1
        app._on_utterance(wake_phrase())
        await request
        assert done == ["answered"] and app._held is None and app.player.duck == 1.0
        assert app.brain.asked == []
    asyncio.run(run())


def test_the_answer_waits_while_the_name_is_checked():
    async def run():
        app = make_app("ну и шум тут")
        done = []
        request = asyncio.create_task(slow_request(app, done))
        await asyncio.sleep(0.05)
        app._on_wake()
        await asyncio.sleep(0.4)          # the request is ready, the phrase after the "name" is still being said
        assert done == []
        app._on_utterance(wake_phrase())
        await request
        assert done == ["answered"]
    asyncio.run(run())


def test_a_confirmed_name_cancels_the_request_and_asks_the_new_one():
    async def run():
        app = make_app("Джарвис, который час")
        done = []
        request = asyncio.create_task(slow_request(app, done))
        await asyncio.sleep(0.05)
        app._on_wake()
        app._on_utterance(wake_phrase())
        await asyncio.sleep(0.2)
        assert request.cancelled() and done == []
        assert app.brain.asked == ["который час"]
    asyncio.run(run())


def test_the_button_switches_listening_off():
    async def run():
        app = make_app("")
        app.state = State.LISTENING
        app.wake_up()
        assert app.state is State.IDLE and "wait" in app.listener.calls and app.cues == [earcons.CANCEL]
        app.state = State.THINKING
        app.listen_now()
        assert app.state is State.IDLE
    asyncio.run(run())


def test_started_with_windows_jarvis_stays_silent(tmp_path, monkeypatch):
    """The Startup shortcut starts him with --autostart: no greeting, missed reminders only in the tray."""
    import json
    import time

    from assistant import autostart
    from assistant.skills.timers import main as timers
    from tests.test_skills import FakeApp

    assert "--autostart" in autostart._target()[1]
    path = tmp_path / "timers.json"
    path.write_text(json.dumps([{"id": "1", "label": "чайник", "due": time.time() - 60, "created": time.time() - 600}]),
                    encoding="utf-8")
    monkeypatch.setattr(timers, "FILE", path)
    app = FakeApp()
    app.quiet_start = True
    notes = []
    app.ui.notify = lambda title, text: notes.append(text)
    skill = timers.create()
    skill.name = "timers"
    skill.setup(app)

    async def run():
        await skill.start()
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert notes == ["чайник"] and app.announced == []


class FakeSpeaker:
    def __init__(self):
        self.said = []
        self.speaking = False
        self.last_text = ""

    def stop(self):
        pass

    def recent_text(self):
        return ""

    async def say(self, text):
        self.said.append(text)
        return text

    async def speak(self, source):
        text = "".join([d async for d in source])
        self.said.append(text)
        return text


class SlowBrain(FakeBrain):
    """The first question searches for a while (its answer streams late); the next ones answer at once."""

    def __init__(self, delay):
        super().__init__()
        self.delay = delay

    async def handle(self, text, addressed=True):
        self.asked.append(text)
        if len(self.asked) == 1:
            async def stream():
                await asyncio.sleep(self.delay)
                yield "В Москве плюс пять."
            return Reply(stream=stream(), listen_after=False)
        return Reply("Сейчас 12:30.", listen_after=False)


def parallel_app(monkeypatch, delay=0.4):
    import assistant.assistant as module

    monkeypatch.setattr(module, "BACKGROUND_GAP_SEC", 0.1)
    app = make_app("")
    app.speaker = FakeSpeaker()
    app.brain = SlowBrain(delay)
    app.llm = type("Llm", (), {"last_provider": "groq"})()
    app.cfg.voice.wait_phrase_sec = 0
    app.react = lambda reaction: asyncio.sleep(0, False)
    return app


def test_a_new_question_does_not_cancel_a_slow_search(monkeypatch):
    """The weather is still being searched when the user asks the time: both are answered, the late one says
    it is about the previous question."""
    async def run():
        app = parallel_app(monkeypatch)
        first = asyncio.create_task(app.handle_text("какая погода в москве"))
        await asyncio.sleep(0.1)
        second = asyncio.create_task(app.handle_text("который час"))
        await asyncio.gather(first, second)
        return app.speaker.said
    said = asyncio.run(run())
    assert said == ["Сейчас 12:30.", "По прошлому вопросу. В Москве плюс пять."]


def test_the_late_answer_waits_while_the_user_is_still_speaking(monkeypatch):
    async def run():
        app = parallel_app(monkeypatch, delay=0.1)
        first = asyncio.create_task(app.handle_text("какая погода в москве"))
        await asyncio.sleep(0.05)
        app._to_background(first)          # the name was said over it: the user is asking something new
        app._task = None
        app.state = State.LISTENING          # ... and is still speaking when the answer is ready
        await asyncio.sleep(0.4)
        assert app.speaker.said == []
        app.state = State.THINKING
        await first
        return app.speaker.said
    assert asyncio.run(run()) == ["По прошлому вопросу. В Москве плюс пять."]


def test_stop_cancels_the_search_too(monkeypatch):
    async def run():
        app = parallel_app(monkeypatch)
        first = asyncio.create_task(app.handle_text("какая погода в москве"))
        await asyncio.sleep(0.1)
        await app.handle_text("стоп")
        await asyncio.sleep(0.5)
        return first.cancelled(), app.speaker.said
    cancelled, said = asyncio.run(run())
    assert cancelled and "В Москве плюс пять." not in " ".join(said)


def test_a_long_search_says_it_is_searching(monkeypatch):
    from assistant.assistant import WAIT_PHRASES

    async def run():
        app = parallel_app(monkeypatch, delay=0.4)
        app.cfg.voice.wait_phrase_sec = 0.1
        await app.handle_text("какая погода в москве")
        return app.speaker.said
    said = asyncio.run(run())
    assert said[0] in WAIT_PHRASES and said[1] == "В Москве плюс пять."


def test_an_obvious_web_question_says_it_is_searching_before_routing(monkeypatch):
    """«сколько стоит …»: «Ищу, сэр» while the router is still thinking, and only once."""
    from assistant.assistant import WAIT_PHRASES

    class RoutingBrain(FakeBrain):
        def surely_web(self, text):
            return True

        async def handle(self, text, addressed=True):
            await asyncio.sleep(0.6)                      # the router
            heard_before.extend(self.speaker.said)

            async def stream():
                await asyncio.sleep(0.6)                  # the search
                yield "Около ста тысяч."
            return Reply(stream=stream(), listen_after=False, searching=True)

    heard_before = []

    async def run():
        app = parallel_app(monkeypatch)
        app.cfg.voice.wait_phrase_sec = 2.5
        app.brain = RoutingBrain()
        app.brain.speaker = app.speaker
        await app.handle_text("сколько стоит айфон 17")
        return app.speaker.said
    said = asyncio.run(run())
    assert heard_before and heard_before[0] in WAIT_PHRASES
    assert said == [heard_before[0], "Около ста тысяч."]
