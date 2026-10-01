"""Brain + router behaviour with a fake model: compound commands, learned phrases, context, redo, answer cache."""
import asyncio
import json

import pytest

import assistant.router as router_mod
from assistant.brain import Brain, Dialog
from assistant.nlu import split_compound
from assistant.skills.base import Intent, Reply, Skill
from tests.test_skills import FakeApp


class Recorder(Skill):
    """A skill that records what it was asked to do."""

    def __init__(self, name, words):
        self.name = name
        self.words = words
        self.calls = []

    def match(self, text):
        for w in self.words:
            if text.startswith(w):
                return Intent(self.name, w, {"rest": text[len(w):].strip()}, text)
        return None

    async def handle(self, intent):
        self.calls.append((intent.action, intent.slots.get("rest", "")))
        return Reply(f"{self.name}:{intent.action}", reaction="ok")


class FakeLocal:
    healthy = True

    def __init__(self):
        self.answers = []
        self.prompts = []

    async def complete(self, messages, fmt=None, max_tokens=None):
        self.prompts.append(messages[-1].content)
        return self.answers.pop(0)

    async def stream_with_tools(self, messages, tools, turn, max_tokens=None):
        yield "локальный ответ"


class FakeHub:
    def __init__(self):
        self.local = FakeLocal()
        self.last_provider = ""
        self.cloud_calls = 0

    async def stream(self, chain, messages, *, web, max_tokens=None):
        self.cloud_calls += 1
        self.last_provider = "groq"
        yield "облачный ответ"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(router_mod, "PHRASEBOOK", tmp_path / "phrasebook.json")
    monkeypatch.setattr(router_mod, "ANSWERS", tmp_path / "answers.json")
    app = FakeApp()
    media = Recorder("media", ["громче", "тише"])
    apps = Recorder("apps", ["открой", "закрой"])
    app.skills = [media, apps]
    for s in app.skills:
        s.setup(app)
    app.llm = FakeHub()
    app.dialog = Dialog(180, 6)
    app.brain = Brain(app, app.skills)
    return app, media, apps


def run(coro):
    return asyncio.run(coro)


async def drain(reply):
    if reply.stream is None:
        return reply.speech
    return "".join([d async for d in reply.stream])


def test_split_compound():
    assert split_compound("закрой браузер и включи музыку") == ["закрой браузер", "включи музыку"]
    assert split_compound("добавь в список покупок молоко и хлеб") == ["добавь в список покупок молоко и хлеб"]
    assert split_compound("открой телеграм а потом сверни все") == ["открой телеграм", "сверни все"]


def test_compound_runs_both(env):
    app, media, apps = env
    reply = run(app.brain.handle("закрой браузер и громче"))
    assert apps.calls == [("закрой", "браузер")] and media.calls == [("громче", "")]
    assert reply.reaction == "ok"


def test_router_maps_and_learns(env):
    app, media, apps = env
    answer = json.dumps({"kind": "commands", "commands": ["громче"], "context": False})
    app.llm.local.answers = [answer, answer]
    run(app.brain.handle("сделай чтобы было слышнее"))
    assert media.calls == [("громче", "")]
    assert "сделай чтобы было слышнее" not in app.brain.router.phrasebook.data  # one answer is not enough
    run(app.brain.handle("сделай чтобы было слышнее"))
    assert "сделай чтобы было слышнее" in app.brain.router.phrasebook.data   # the same answer twice: learned
    # third time: the phrasebook answers, the model is not asked
    run(app.brain.handle("сделай чтобы было слышнее"))
    assert media.calls == [("громче", "")] * 3
    assert len(app.llm.local.prompts) == 2


def test_router_context_not_learned(env):
    app, media, apps = env
    app.llm.local.answers = [json.dumps({"kind": "commands", "commands": ["закрой телеграм"], "context": True})]
    run(app.brain.handle("а теперь закрой его"))
    assert apps.calls == [("закрой", "телеграм")]
    assert "а теперь закрой его" not in app.brain.router.phrasebook.data


def test_router_sees_recent_turns(env):
    app, media, apps = env
    run(app.brain.handle("открой телеграм"))
    app.llm.local.answers = [json.dumps({"kind": "chat", "query": "что такое телеграм"})]
    run(app.brain.handle("что это вообще такое"))
    assert "открой телеграм" in app.llm.local.prompts[0]


def test_unknown_router_command_falls_back_to_chat(env):
    app, media, apps = env
    app.llm.local.answers = [json.dumps({"kind": "commands", "commands": ["станцуй"]})]
    reply = run(app.brain.handle("станцуй для меня"))
    assert run(drain(reply)) == "локальный ответ"


def test_web_answer_is_cached(env):
    app, media, apps = env
    app.llm.local.answers = [json.dumps({"kind": "web", "query": "курс доллара", "fresh": True}),
                             json.dumps({"kind": "web", "query": "курс доллара", "fresh": True})]
    first = run(drain(run(app.brain.handle("какой там курс бакса"))))
    second = run(drain(run(app.brain.handle("а курс доллара сейчас"))))
    assert first == second == "облачный ответ"
    assert app.llm.cloud_calls == 1  # the second answer came from the cache


def test_redo_repeats_last_command(env):
    app, media, apps = env
    run(app.brain.handle("громче"))
    run(app.brain.redo())
    assert media.calls == [("громче", ""), ("громче", "")]


def test_router_disabled_uses_heuristic(env):
    app, media, apps = env
    app.cfg.router.enabled = False
    reply = run(app.brain.handle("что такое фотосинтез"))
    assert run(drain(reply)) == "облачный ответ"
