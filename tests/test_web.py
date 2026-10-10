"""Browser agent: safety rules, routes, the extension link, phrases and the agent loop with a fake browser."""
import asyncio
import json

import pytest

from assistant.web import policy
from assistant.web.agent import Job, WebAgent, format_snapshot, parse_json
from assistant.web.bridge import Bridge, BridgeError, ensure_key, proof
from assistant.web.routes import Routes

from tests.test_skills import FakeApp, route


@pytest.fixture(scope="module")
def jarvis():
    """All modules in production order (the first match wins)."""
    from assistant.brain import Brain
    from assistant.skills.base import load_skills

    a = FakeApp()
    a.skills = load_skills(a.cfg.skills.enabled)
    for s in a.skills:
        s.setup(a)
    a.brain = Brain(a, a.skills)
    return a


# ------------------------------------------------------------------ policy
@pytest.mark.parametrize("task", [
    "переведи 500 рублей маме на карту", "отправь деньги Саше по номеру телефона", "зайди в госуслуги и проверь штрафы",
    "войди в сбер и оплати кредит", "купи биткоин на бирже", "введи мой пароль от почты", "реши капчу",
])
def test_tasks_never_done(task):
    assert policy.check_task(task)


@pytest.mark.parametrize("task", [
    "найди на озоне наушники до 3000", "сравни цены на айфон 15", "включи на ютубе обзор 4060",
    "положи в корзину на вб зарядку", "перескажи эту статью",
])
def test_ordinary_tasks_allowed(task):
    assert policy.check_task(task) == ""


def test_forbidden_sites():
    assert policy.forbidden_site("https://online.sberbank.ru/login")
    assert policy.forbidden_site("https://www.gosuslugi.ru/")
    assert policy.forbidden_site("https://3ds.payment.example.com/acs")
    assert policy.forbidden_site("https://www.ozon.ru/search/?text=x") == ""


def el(**kw):
    return {"id": 1, "tag": "button", **kw}


@pytest.mark.parametrize("element,kind", [
    (el(text="В корзину"), "allow"),
    (el(text="Добавить в избранное"), "allow"),
    (el(text="Показать фильтры"), "allow"),
    (el(text="Оформить заказ"), "confirm"),
    (el(text="Оплатить 2 990 ₽"), "confirm"),
    (el(text="Отправить отзыв"), "confirm"),
    (el(text="Удалить из корзины"), "confirm"),
    (el(text="Подписаться"), "confirm"),
    (el(text="Принять условия"), "never"),
    (el(text="Принять все cookies"), "never"),
    (el(text="Отклонить необязательные"), "allow"),
])
def test_clicks(element, kind):
    assert policy.judge({"action": "click", "id": 1}, element, "https://www.ozon.ru/").kind == kind


def test_typing():
    search = {"id": 2, "tag": "input", "type": "text", "search": True, "form": True, "placeholder": "Искать на Ozon"}
    assert policy.judge({"action": "type", "id": 2, "text": "наушники", "submit": True}, search, "").kind == "allow"
    pw = {"id": 3, "tag": "input", "type": "password", "name": "pass"}
    assert policy.judge({"action": "type", "id": 3, "text": "x"}, pw, "").kind == "never"
    card = {"id": 4, "tag": "input", "type": "text", "ac": "cc-number", "placeholder": "Номер карты"}
    assert policy.judge({"action": "type", "id": 4, "text": "1"}, card, "").kind == "never"
    sms = {"id": 5, "tag": "input", "type": "text", "ac": "one-time-code"}
    assert policy.judge({"action": "type", "id": 5, "text": "1"}, sms, "").kind == "never"
    comment = {"id": 6, "tag": "textarea", "form": True, "placeholder": "Ваш комментарий"}
    assert policy.judge({"action": "type", "id": 6, "text": "Спасибо", "submit": True}, comment, "").kind == "confirm"
    assert policy.judge({"action": "goto", "url": "https://www.tbank.ru/"}, None, "").kind == "never"
    assert policy.judge({"action": "goto", "url": "javascript:alert(1)"}, None, "").kind == "never"


def test_blockers():
    assert policy.blocker({"url": "https://ya.ru", "flags": {"captcha": True}, "text": ""})[0] == "captcha"
    assert policy.blocker({"url": "https://ozon.ru", "title": "Вход", "flags": {"password": True}, "text": "Войти"})[0] \
        == "login"
    assert policy.blocker({"url": "https://www.ozon.ru/", "title": "OZON", "flags": {}, "text": "Наушники"})[0] == ""


# ------------------------------------------------------------------ routes
def test_routes_mentioned_and_search(tmp_path):
    routes = Routes(tmp_path / "r.json")
    assert [s.domain for s in routes.mentioned("найди на озоне наушники")] == ["ozon.ru"]
    assert "wildberries.ru" in [s.domain for s in routes.mentioned("сравни на вб и на маркете")]
    assert routes.search_url("www.ozon.ru", "наушники sony").startswith("https://www.ozon.ru/search/?text=%D0%BD")


def test_routes_learn_new_site(tmp_path):
    routes = Routes(tmp_path / "r.json")
    routes.learn(["https://shop.example.ru/find?query=%D0%BB%D0%B0%D0%BC%D0%BF%D0%B0&page=1"], "лампа",
                 "сортировка по цене — параметр &sort=price")
    again = Routes(tmp_path / "r.json")
    assert again.search_url("shop.example.ru", "стол") == "https://shop.example.ru/find?query=%D1%81%D1%82%D0%BE%D0%BB"
    assert "sort=price" in again.describe(again.mentioned("на shop.example.ru"))


# ------------------------------------------------------------------ the extension link
EXT = "chrome-extension://ojccaehhgmcodmmaklilhcacibehfain"


async def fake_extension(port, key, origin=EXT, answer=None):
    """Plays the extension's side of the handshake and answers commands."""
    from websockets.asyncio.client import connect

    ws = await connect(f"ws://127.0.0.1:{port}/", origin=origin)
    await ws.send(json.dumps({"event": "hello", "version": "test", "nonce": "n1"}))
    welcome = json.loads(await ws.recv())
    assert welcome["proof"] == proof(key, "jarvis:n1")
    await ws.send(json.dumps({"event": "auth", "proof": proof(answer or key, "ext:" + welcome["nonce"])}))
    return ws


def test_bridge_handshake_and_calls(unused_port):
    async def run():
        bridge = Bridge(unused_port, "k" * 64)
        await bridge.start()
        ws = await fake_extension(unused_port, "k" * 64)
        assert await bridge.wait_connected(2)

        async def serve():
            msg = json.loads(await ws.recv())
            await ws.send(json.dumps({"id": msg["id"], "result": {"echo": msg["params"]["x"]}}))
            msg = json.loads(await ws.recv())
            await ws.send(json.dumps({"id": msg["id"], "error": "нет элемента"}))

        server = asyncio.create_task(serve())
        assert await bridge.call("snapshot", x=5) == {"echo": 5}
        with pytest.raises(BridgeError):
            await bridge.call("click", id=1)
        await server
        await ws.close()
        await bridge.stop()

    asyncio.run(run())


def test_bridge_rejects_wrong_key_and_origin(unused_port):
    async def run():
        bridge = Bridge(unused_port, "k" * 64)
        await bridge.start()
        ws = await fake_extension(unused_port, "k" * 64, answer="x" * 64)
        await asyncio.sleep(0.2)
        assert not bridge.connected
        await ws.close()
        from websockets.asyncio.client import connect
        with pytest.raises(Exception):
            await connect(f"ws://127.0.0.1:{unused_port}/", origin="https://evil.example")
        await bridge.stop()

    asyncio.run(run())


def test_pairing_key_written_for_the_extension(tmp_path):
    ext = tmp_path / "ext"
    ext.mkdir()
    key = ensure_key(tmp_path / "web_key", ext)
    assert len(key) == 64
    assert json.loads((ext / "pairing.json").read_text())["key"] == key
    assert ensure_key(tmp_path / "web_key", ext) == key


@pytest.fixture
def unused_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ phrases
WEB_CASES = [
    ("найди на озоне наушники до 3000 с хорошими отзывами", "task"),
    ("сколько стоит айфон 15 на вайлдберриз", "task"),
    ("сравни цены на робот-пылесос на маркете", "task"),
    ("положи в корзину на вб зарядку для айфона", "task"),
    ("включи на ютубе обзор rtx 5070", "task"),
    ("сделай в браузере подборку статей про rust", "task"),
    ("перескажи эту статью", "read"),
    ("кратко перескажи это видео", "read"),
    ("о чем это видео", "read"),
    ("что на этой странице про доставку", "read"),
    ("каждый день в 9 проверяй цену на airpods на озоне", "schedule"),
    ("следи за ценой на пылесос dreame на маркете", "schedule"),
    ("какие проверки в браузере", "jobs"),
    ("отмени проверку цены на airpods", "unschedule"),
    ("покажи браузер", "show"),
    ("останови задачу в браузере", "cancel"),
    ("хватит искать", "cancel"),
    ("подключи браузер", "setup"),
]


@pytest.mark.parametrize("phrase,action", WEB_CASES)
def test_web_phrases(jarvis, phrase, action):
    assert route(jarvis, phrase) == ("web", action), phrase


@pytest.mark.parametrize("phrase,skill", [
    ("найди на ютубе обзор 4060", "search"), ("найди в браузере курс python", "search"),
    ("включи звук в браузере", "media"), ("закрой браузер", "windows"), ("открой браузер", "apps"),
    ("перескажи", "selection"), ("о чем статья", "selection"), ("зайди на habr.com", "apps"),
])
def test_other_modules_keep_their_phrases(jarvis, phrase, skill):
    hit = route(jarvis, phrase)
    assert hit is not None and hit[0] == skill, (phrase, hit)


# ------------------------------------------------------------------ the agent loop
class FakeBridge:
    """A shop page with a search box and two products."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.url = ""

    connected = True

    def notify(self, event, **data):
        self.calls.append((f"notify:{event}", data))

    async def call(self, method, timeout=40, **p):
        self.calls.append((method, p))
        if method == "open":
            self.url = p["url"]
            return {"url": self.url, "title": "Озон"}
        if method == "snapshot":
            return self.pages[min(len([c for c in self.calls if c[0] == "snapshot"]) - 1, len(self.pages) - 1)]
        if method == "readable":
            return {"url": self.url, "title": "Озон", "text": "Наушники Sony WH-1000XM5 — 24 990 ₽ — 4.9 (1200)"}
        if method == "links":
            return [{"text": "Наушники Sony WH-1000XM5", "href": "https://www.ozon.ru/product/sony-1/"}]
        if method == "agent_tab":
            return {"url": self.url, "title": "Озон"}
        if method in ("click", "type"):
            return {"ok": True, "url": self.url}
        return {"ok": True}


SHOP_PAGE = {"url": "https://www.ozon.ru/search/?text=наушники", "title": "Наушники — OZON", "flags": {},
             "text": "Наушники Sony WH-1000XM5 24 990 ₽", "textTotal": 40,
             "elements": [{"id": 1, "tag": "a", "text": "Наушники Sony WH-1000XM5", "href": "https://www.ozon.ru/product/sony-1/",
                           "inView": True},
                          {"id": 2, "tag": "button", "text": "Оформить заказ", "inView": True}]}


class FakeLocal:
    healthy = True

    def __init__(self, steps):
        self.steps = list(steps)

    async def complete(self, messages, fmt=None, max_tokens=None, model=""):
        return json.dumps(self.steps.pop(0))


class FakeLlm:
    def __init__(self, steps):
        self.local = FakeLocal(steps)
        self.prompts = []

    def status(self):
        return {"gemini": "готов"}

    async def complete(self, chain, messages, *, web, max_tokens=None, models=None):
        prompt = messages[-1].content
        self.prompts.append(prompt)
        if "Ты планируешь" in prompt:
            return json.dumps({"start_url": "https://www.ozon.ru/search/?text=наушники", "query": "наушники",
                               "goal": "подборка наушников", "steps": ["найти", "сравнить"], "result": "list"})
        if "Выпиши со страницы" in prompt:
            return "Sony WH-1000XM5 — 24 990 ₽ — 4.9 (1200) — https://www.ozon.ru/product/sony-1/"
        if "Нужно ли сообщить" in prompt:
            return json.dumps({"notify": False, "speech": ""})
        return json.dumps({"speech": "Лучшие — Sony WH-1000XM5 за 24 990 рублей.", "title": "Наушники",
                           "card": "- [Sony WH-1000XM5](https://www.ozon.ru/product/sony-1/) — 24 990 ₽, 4.9",
                           "lesson": "", "summary": "Sony 24990"})


class AgentApp:
    def __init__(self, steps, answer=True):
        from assistant.config import load_settings
        from assistant.core import State

        self.cfg = load_settings()
        self.llm = FakeLlm(steps)
        self.state = State.IDLE
        self.questions = []
        self.announced = []
        self.answer = answer

    async def ask_yes_no(self, question, timeout=45):
        self.questions.append(question)
        return self.answer

    async def announce(self, text, cue=None):
        self.announced.append(text)


def make_agent(steps, pages=(SHOP_PAGE,), answer=True, tmp_path=None):
    app = AgentApp(steps, answer)
    bridge = FakeBridge(list(pages))
    return WebAgent(app, bridge, Routes(tmp_path / "routes.json")), app, bridge


def test_agent_reads_results_and_answers(tmp_path):
    agent, app, bridge = make_agent([{"action": "extract", "why": "товары на странице"}], tmp_path=tmp_path)
    outcome = asyncio.run(agent.run(Job("найди на озоне наушники")))
    assert outcome.ok and "Sony" in outcome.speech and "](https://www.ozon.ru/product/sony-1/)" in outcome.card
    opened = next(c for c in bridge.calls if c[0] == "open")
    assert "ozon.ru/search" in opened[1]["url"] and opened[1]["mode"] == "tab"
    assert not app.questions
    progress = [c[1]["text"] for c in bridge.calls if c[0] == "notify:progress"]
    assert progress[0] == "планирую" and progress[-1] == ""      # the extension shows each step, then clears


def test_agent_asks_before_checkout_and_stops_on_no(tmp_path):
    agent, app, bridge = make_agent([{"action": "click", "id": 2, "why": "оформить"}], answer=False, tmp_path=tmp_path)
    outcome = asyncio.run(agent.run(Job("закажи на озоне наушники sony")))
    assert app.questions and "Оформить заказ" in app.questions[0]
    assert not outcome.ok and not any(c[0] == "click" for c in bridge.calls)


def test_agent_clicks_after_yes(tmp_path):
    steps = [{"action": "click", "id": 2, "why": "оформить"}, {"action": "done", "why": "заказ оформлен"}]
    agent, app, bridge = make_agent(steps, answer=True, tmp_path=tmp_path)
    asyncio.run(agent.run(Job("закажи на озоне наушники sony")))
    assert ("click", {"id": 2}) in bridge.calls


def test_scheduled_check_never_asks(tmp_path):
    agent, app, bridge = make_agent([{"action": "click", "id": 2, "why": "оформить"}], tmp_path=tmp_path)
    outcome = asyncio.run(agent.run(Job("закажи на озоне наушники", quiet=True)))
    assert not app.questions and not outcome.ok


def test_agent_hands_captcha_to_the_user(tmp_path):
    captcha = {**SHOP_PAGE, "flags": {"captcha": True}, "elements": []}
    agent, app, bridge = make_agent([{"action": "extract", "why": "товары"}], pages=(captcha, SHOP_PAGE),
                                    tmp_path=tmp_path)
    app.cfg.web.handover_wait_sec = 10

    async def run():
        import assistant.web.agent as mod
        real_sleep = asyncio.sleep
        mod.asyncio.sleep = lambda s: real_sleep(0)
        try:
            return await agent.run(Job("найди на озоне наушники"))
        finally:
            mod.asyncio.sleep = real_sleep

    outcome = asyncio.run(run())
    assert app.announced and "робот" in app.announced[0]
    assert ("show", {}) in bridge.calls and outcome.ok


def test_refused_task_never_opens_the_browser(tmp_path):
    agent, app, bridge = make_agent([], tmp_path=tmp_path)
    outcome = asyncio.run(agent.run(Job("переведи 1000 рублей Пете на карту")))
    assert not outcome.ok and not [c for c in bridge.calls if not c[0].startswith("notify:")]


def test_snapshot_text_marks_page_as_data():
    text = format_snapshot(SHOP_PAGE)
    assert "[1] ссылка «Наушники Sony WH-1000XM5» → ozon.ru/product/sony-1/" in text
    assert "[2] кнопка «Оформить заказ»" in text
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_read_says_reading_at_once_then_the_summary(jarvis):
    from assistant.web.agent import Outcome

    skill = next(s for s in jarvis.skills if s.name == "web")

    class Agent:
        async def read_current(self, question):
            return Outcome(True, "Статья о том, как выбрать наушники.", "Наушники", "- Главное: звук", "https://a.ru/x")

    skill.agent = Agent()
    shown = []
    jarvis.ui.show_deck = shown.append

    async def run():
        reply = await skill._read("перескажи эту статью")
        return [part async for part in reply.stream]

    parts = asyncio.run(run())
    assert parts[0] == "Читаю. " and "наушники" in parts[1]
    assert shown and shown[0].pinned and "Главное" in shown[0].cards[0].screen
    skill.agent = None


def test_voice_mute_stays_muted_after_the_answer():
    """"выключи микрофон" is answered aloud; the end of the answer must not flip the tray back to "Жду"."""
    from assistant.assistant import Assistant
    from assistant.core import ConsoleUi, State

    app = Assistant.__new__(Assistant)
    app.ui = ConsoleUi()
    app.state_listeners = []
    app.music = type("M", (), {"duck": lambda self, on: None})()
    app.mic = type("Mic", (), {"paused": True})()
    app._dictating = False
    app._set_state(State.IDLE)
    assert app.state is State.MUTED
    app.mic.paused = False
    app._set_state(State.IDLE)
    assert app.state is State.IDLE


# ------------------------------------------------------------------ "да" / "нет" for background questions
def test_pending_deny_runs_on_no_and_on_other_phrases(jarvis):
    import time

    from assistant.skills.base import Reply

    app = jarvis
    said = []

    async def yes():
        said.append("yes")
        return Reply("ok")

    async def no():
        said.append("no")
        return Reply("Хорошо, не буду.")

    app.brain.pending = (yes, time.monotonic() + 30, no)
    reply = asyncio.run(app.brain._resolve_pending("нет"))
    assert said == ["no"] and reply.speech == "Хорошо, не буду."
    app.brain.pending = (yes, time.monotonic() + 30, no)
    assert asyncio.run(app.brain._resolve_pending("какая погода")) is None and said == ["no", "no"]
    app.brain.pending = (yes, time.monotonic() + 30, no)
    asyncio.run(app.brain._resolve_pending("да"))
    assert said[-1] == "yes"
