"""«Не то» / «верни как было», phrases not meant for Jarvis, clarifying questions, cloud limits, hot-window filters."""
import json
from types import SimpleNamespace

import numpy as np

from assistant.assistant import Assistant
from assistant.llm.hub import cooldown_for
from assistant.llm.providers import QuotaError, parse_wait
from assistant.nlu import find_wake, is_bare_wake, is_echo, strip_echo
from assistant.skills.base import Intent, Reply, Skill
from tests import test_router
from tests.test_router import run

env = test_router.env  # the brain + router fixture
PHRASES = ["джарвис", "джервис"]


class Volume(Skill):
    """Remembers the level and knows how to take a change back."""

    name = "media"

    def __init__(self):
        self.level = 50

    def match(self, text):
        return Intent(self.name, "up", text=text) if text.startswith("громче") else None

    async def handle(self, intent):
        self.level += 10

        async def undo():
            self.level -= 10
            return Reply("Вернул громкость.")

        return Reply(listen_after=False, undo=undo)


def test_wrong_undoes_and_forgets_router_phrase(env):
    app, media, apps = env
    vol = Volume()
    vol.setup(app)
    app.skills.insert(0, vol)
    app.brain.skills = app.skills
    answer = json.dumps({"kind": "commands", "commands": ["громче"], "context": False})
    app.llm.local.answers = [answer, answer]
    run(app.brain.handle("сделай чтобы было слышнее"))
    run(app.brain.handle("сделай чтобы было слышнее"))
    assert vol.level == 70 and "сделай чтобы было слышнее" in app.brain.router.phrasebook.data
    reply = run(app.brain.undo_last(forget=True))
    assert vol.level == 60
    assert "сделай чтобы было слышнее" not in app.brain.router.phrasebook.data
    assert "забыл" in reply.speech


def test_undo_takes_back_without_forgetting(env):
    app, media, apps = env
    vol = Volume()
    vol.setup(app)
    app.skills.insert(0, vol)
    app.brain.skills = app.skills
    run(app.brain.handle("громче"))
    assert vol.level == 60
    assert run(app.brain.undo_last(forget=False)).speech == "Вернул громкость."
    assert vol.level == 50
    assert run(app.brain.undo_last(forget=False)).speech == "Здесь нечего отменять."


def test_hot_window_phrase_for_someone_else_is_ignored(env):
    app, media, apps = env
    app.llm.local.answers = [json.dumps({"kind": "ignore"})]
    reply = run(app.brain.handle("ну и что он тебе сказал", addressed=False))
    assert reply.spoken and not reply.listen_after
    assert "сказано не мне" in "сказано не мне"  # the router got the hint below
    assert "БЕЗ обращения по имени" in app.llm.local.prompts[0]


def test_garbage_after_the_name_is_not_guessed(env):
    app, media, apps = env
    app.llm.local.answers = [json.dumps({"kind": "ignore"})]
    reply = run(app.brain.handle("жарес"))
    assert reply.speech == "Не расслышал, повторите." and not media.calls and not apps.calls


def test_clarifying_question_takes_the_next_phrase(env):
    app, media, apps = env
    chosen = []

    async def answer(text):
        if "хром" in text:
            chosen.append("chrome")
            return Reply("Закрываю Chrome.")
        return None

    app.brain._remember_confirm(Reply("Закрыть Chrome или Яндекс Браузер?", ask=answer))
    assert app.brain.expects_answer()
    assert run(app.brain.handle("хром")).speech == "Закрываю Chrome."
    assert chosen == ["chrome"] and not app.brain.expects_answer()


def test_not_an_answer_goes_on_as_a_request(env):
    app, media, apps = env

    async def answer(text):
        return None

    app.brain._remember_confirm(Reply("Закрыть Chrome или Яндекс Браузер?", ask=answer))
    run(app.brain.handle("громче"))
    assert media.calls == [("громче", "")]


def test_groq_minute_limit_waits_seconds_not_minutes():
    msg = ("Rate limit reached for model openai/gpt-oss-120b on tokens per minute (TPM): Limit 8000, Used 6100, "
           "Requested 3900. Please try again in 14.25s.")
    assert parse_wait(msg) == 14.25
    assert cooldown_for(QuotaError(msg, 14.25, False), 900) == 15.25
    assert parse_wait("Please try again in 2m30.5s") == 150.5
    assert parse_wait("'retryDelay': '17s'") == 17
    assert cooldown_for(QuotaError("x", None, False), 900) == 900


def test_wake_name_variants_and_bare_wake():
    for heard in ["Жарвис.", "Ярвес.", "Жервес.", "Жарес.", "Джарвис?"]:
        assert is_bare_wake(heard, PHRASES), heard
    assert not is_bare_wake("Жарвис, включи музыку", PHRASES)
    assert find_wake("Джарлис, расскажи шутку.", PHRASES, 70) is not None
    assert find_wake("Жарко сегодня", PHRASES, 70) is None
    # the best match is the name, not a similar word
    span = find_wake("Расскажи про Дарвина, Джарвис", PHRASES, 70)
    assert "Расскажи про Дарвина, Джарвис"[span[0]:span[1]] == "Джарвис"


def test_echo_of_own_answer():
    said = "Курс доллара — 84,43 ₽ за 1 USD."
    assert is_echo("84, 3", said) and is_echo("Курс доллара.", said)
    assert not is_echo("курс евро", said) and not is_echo("какая погода", said)
    assert strip_echo("Да, сэр. Сколько треков в музыке?", "Да-сэр.") == "Сколько треков в музыке?"
    assert strip_echo("Слушаю. Открой телеграм", "Слушаю.") == "Открой телеграм"


def fake_assistant(last_text=""):
    cfg = SimpleNamespace(wake=SimpleNamespace(phrases=PHRASES, verify_ratio=70))
    return SimpleNamespace(cfg=cfg, speaker=SimpleNamespace(last_text=last_text))


def test_hot_window_filters():
    fa = fake_assistant("Курс доллара — 84,43 ₽ за 1 USD.")
    utt = SimpleNamespace(voiced_sec=1.2)
    res = SimpleNamespace(text="А завтра?", confidence=0.95)
    assert not Assistant._not_for_me(fa, utt, res)
    assert Assistant._not_for_me(fa, SimpleNamespace(voiced_sec=0.3), res)          # a cough, a click
    assert Assistant._not_for_me(fa, utt, SimpleNamespace(text="мда", confidence=0.4))  # mumbling
    assert Assistant._not_for_me(fa, utt, SimpleNamespace(text="84, 3", confidence=0.9))  # own echo


def test_barge_in_keeps_the_command_after_the_name():
    fa = fake_assistant("Сегодня ясно, днём до плюс двенадцати.")
    assert Assistant._after_name(fa, "днём до плюс Джарвис, какой курс евро") == "Джарвис, какой курс евро"
    # the name at the end: Jarvis's own words before it are dropped
    assert Assistant._after_name(fa, "Сегодня ясно Включи музыку, Джарвис") == "Включи музыку, Джарвис"


def test_listener_marks_barge_in_and_hot_source():
    from assistant.audio.listener import ListenerCore, ListenerEvents
    from assistant.audio.vad import FRAME

    class Vad:
        def __call__(self, frame):
            return 1.0 if np.abs(frame).max() > 1000 else 0.0

    class Wake:
        def __init__(self):
            self.n = 0

        def feed_wake(self, pcm):
            self.n += 1
            return self.n == 120  # ~3.8 s into the speech

        def feed_stop(self, pcm):
            return None

        def reset(self):
            pass

    utts = []
    core = ListenerCore(Vad(), Wake(), ListenerEvents(lambda: None, lambda w, a: None, lambda: None, utts.append,
                                                      lambda: None), end_silence_ms=500, wake_grace_ms=500)
    core.assistant_speaking = True
    loud, quiet = np.full(FRAME, 5000, np.int16), np.zeros(FRAME, np.int16)
    for _ in range(180):
        core.process(loud)
    for _ in range(40):
        core.process(quiet)
    assert len(utts) == 1 and utts[0].barge_in and utts[0].source == "wake"
    # only ~2 s before the name were kept (Jarvis was talking before it), not the whole 3.8 s
    assert utts[0].audio.size / 16000 < 2.0 + 60 * FRAME / 16000 + 40 * FRAME / 16000 + 0.1
    assert utts[0].voiced_sec > 1.5
    core.assistant_speaking = False
    core.to_await(5, "hot")
    for _ in range(30):
        core.process(loud)
    for _ in range(40):
        core.process(quiet)
    assert utts[-1].source == "hot"
