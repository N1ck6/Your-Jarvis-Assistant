"""The user's earlier requests: "какой был мой прошлый запрос", "а до этого?", "не то, я имел в виду …",
"давай ещё раз", and a solved task changed afterwards ("а если скорость 80?", "почему так?")."""
import asyncio

import pytest

import assistant.router as router_mod
from assistant.brain import Brain, Dialog
from assistant.skills.base import Reply
from assistant.skills.system.main import SystemSkill
from tests.test_router import FakeHub, Recorder, drain
from tests.test_skills import FakeApp, SOLVED
from tests.test_skills import app  # noqa: F401  (the fixture with every real skill)


class Undoable(Recorder):
    """Records actions and their undos."""

    def __init__(self, name, words):
        super().__init__(name, words)
        self.undone = []

    async def handle(self, intent):
        reply = await super().handle(intent)
        action = (intent.action, intent.slots.get("rest", ""))

        async def undo():
            self.undone.append(action)
            return Reply("Отменил.")

        reply.undo = undo
        return reply


class SeeingHub(FakeHub):
    """Keeps what the local model was shown as its system prompt."""

    def __init__(self):
        super().__init__()
        self.systems = []
        hub = self

        async def stream_with_tools(messages, tools, turn, max_tokens=None):
            hub.systems.append(messages[0].content)
            hub.asked = messages[-1].content
            yield "локальный ответ"

        self.local.stream_with_tools = stream_with_tools


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(router_mod, "PHRASEBOOK", tmp_path / "phrasebook.json")
    monkeypatch.setattr(router_mod, "ANSWERS", tmp_path / "answers.json")
    a = FakeApp()
    system = SystemSkill()
    system.name = "system"
    media = Recorder("media", ["громче", "тише"])
    apps = Undoable("apps", ["открой", "закрой"])
    a.skills = [system, media, apps]
    for s in a.skills:
        s.setup(a)
    a.llm = SeeingHub()
    a.dialog = Dialog(180, 6)
    a.brain = Brain(a, a.skills)
    return a, media, apps


def run(coro):
    return asyncio.run(coro)


def say(a, phrase):
    return run(drain(run(a.brain.handle(phrase))))


def test_previous_request_is_the_last_one_not_an_older_one(env):
    """Commands count: the chat history hides them from the model, the request history does not."""
    a, media, apps = env
    say(a, "открой телеграм")
    say(a, "громче")
    assert say(a, "Каков был мой предыдущий запрос?") == "Вы попросили: «громче»."
    assert say(a, "а до этого?") == "Вы до этого попросили: «открой телеграм»."
    assert say(a, "а до этого") == "Раньше этого запросов не помню."
    # looking into the history is not a request itself
    assert say(a, "какой был мой прошлый запрос") == "Вы попросили: «громче»."


def test_previous_question_and_its_rewrite(env):
    a, media, apps = env
    a.llm.local.answers = ["C: что такое DNS"]
    say(a, "что такое DNS")
    assert say(a, "что я спрашивал до этого") == "Вы спросили: «что такое DNS»."
    a.llm.local.answers = ["C: что такое DNS простыми словами"]
    say(a, "а попроще?")
    assert say(a, "мой прошлый вопрос") == "Вы спросили: «а попроще». То есть: что такое DNS простыми словами."


def test_nothing_asked_yet(env):
    a, media, apps = env
    assert say(a, "какой был мой прошлый запрос") == "Вы ещё ничего не просили."


def test_recalled_request_runs_again_or_with_a_change(env):
    a, media, apps = env
    say(a, "громче")
    say(a, "открой телеграм")
    say(a, "что я просил до этого")
    say(a, "а перед этим")             # -> «громче»
    say(a, "давай ещё раз")
    assert media.calls == [("громче", ""), ("громче", "")]
    say(a, "какой был мой прошлый запрос")   # «громче» again: the rerun is the newest request now
    say(a, "то же самое, но открой браузер")
    assert apps.calls[-1] == ("открой", "браузер")
    assert apps.undone == []           # a variant, not a correction: nothing is taken back


def test_rerun_previous_request(env):
    a, media, apps = env
    say(a, "громче")
    say(a, "повтори мой прошлый запрос")
    assert media.calls == [("громче", ""), ("громче", "")]


def test_correction_takes_the_action_back_and_does_the_right_one(env):
    a, media, apps = env
    say(a, "открой телеграм")
    say(a, "не то, я имел в виду закрой телеграм")
    assert apps.undone == [("открой", "телеграм")]
    assert apps.calls == [("открой", "телеграм"), ("закрой", "телеграм")]
    assert say(a, "какой был мой прошлый запрос") == "Вы попросили: «закрой телеграм»."


def test_correction_of_a_question_goes_to_the_router_with_the_old_question(env):
    a, media, apps = env
    a.llm.local.answers = ["C: что такое питон", "C: что такое язык программирования Python"]
    say(a, "что такое питон")
    assert say(a, "нет, я имел в виду язык программирования") == "локальный ответ"
    assert "поправка к запросу «что такое питон»" in a.llm.local.prompts[-1]
    assert a.llm.asked.endswith("(Смысл запроса с учётом прошлых: что такое язык программирования Python)")


def test_not_that_then_the_correction(env):
    """"не то" -> "Уточните…" -> the next phrase corrects the same request."""
    a, media, apps = env
    a.llm.local.answers = ["C: что такое питон", "C: что такое язык программирования Python"]
    say(a, "что такое питон")
    assert say(a, "не то") == "Уточните, что вы имели в виду."
    say(a, "язык программирования")
    assert "поправка к запросу «что такое питон»" in a.llm.local.prompts[-1]


def test_rewritten_follow_up_that_is_no_command_is_asked_as_a_question(env):
    """The router sometimes returns the rewritten question as a "command": it is still asked, with its context."""
    a, media, apps = env
    a.llm.local.answers = ["C: как отсортировать список в javascript", "К+: как отсортировать список в python"]
    say(a, "как отсортировать список в javascript")
    assert say(a, "а что если на питоне") == "локальный ответ"
    assert "как отсортировать список в python" in a.llm.asked


def test_local_model_sees_commands_too(env):
    a, media, apps = env
    say(a, "открой телеграм")
    a.llm.local.answers = ["C: что я делал"]
    say(a, "а что я делал")
    assert "«открой телеграм»" in a.llm.systems[-1]


def test_request_history(env):
    a, media, apps = env
    say(a, "открой телеграм")
    say(a, "громче")
    assert say(a, "мои последние запросы") == "Последние: «громче», «открой телеграм». Список на экране."


# ---------------------------------------------------------------- a solved task, changed afterwards
@pytest.fixture
def solver(app, monkeypatch):  # noqa: F811
    solve = next(s for s in app.skills if s.name == "solve")
    sent, decks = [], []

    async def complete(chain, messages, **kw):
        sent.append(messages)
        return SOLVED

    monkeypatch.setattr(app, "llm", type("Llm", (), {"complete": staticmethod(complete), "last_provider": "gemini"})(),
                        raising=False)
    monkeypatch.setattr(app.ui, "show_deck", decks.append)
    monkeypatch.setattr(app, "dialog", Dialog(180, 6), raising=False)
    app.brain = Brain(app, app.skills)
    solve.last_run = None
    solve.ctx.share("screen.region", b"png")
    return app, solve, sent, decks


def test_what_if_solves_the_same_task_again_with_the_change(solver):
    app, solve, sent, decks = solver
    assert say(app, "реши эту задачу") == "Решаю. Ответ: 3 часа"
    assert say(app, "а что если скорость будет 80 километров в час") == "Пересчитываю. Ответ: 3 часа"
    prompt = sent[-1][0]
    assert prompt.images == [b"png"]                          # the same picture, not a new region
    assert "скорость будет 80" in prompt.content and "3 часа" in prompt.content   # the change and the old solution
    say(app, "а для двух поездов")
    assert "скорость будет 80" in sent[-1][0].content and "для двух поездов" in sent[-1][0].content   # both changes


def test_wrong_answer_and_why_after_a_solution(solver):
    app, solve, sent, decks = solver
    say(app, "реши задачу: поезд едет 240 км со скоростью 80 км/ч, сколько он в пути")
    say(app, "неправильно")                                   # not "не то": the solution is checked again
    assert "проверь" in sent[-1][0].content and "поезд едет 240 км" in sent[-1][1].content
    assert say(app, "почему так?") == "Объясняю. Расписал решение по шагам на экране."
    assert decks[-1].title == "Как решал"


def test_what_if_without_a_task_is_not_a_solution(solver):
    app, solve, sent, decks = solver
    assert solve.followup("а что если скорость 80", None) is None
