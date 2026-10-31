"""The local model kept fast, clouds that refuse the country, and guessed dangerous commands."""
import asyncio

from assistant.llm import ollama_keeper as keeper
from assistant.llm.hub import LlmHub
from assistant.llm.providers import Msg, Provider, RegionError

GB = 2**30


def test_a_model_pushed_onto_the_cpu_is_reloaded_once_the_gpu_is_free():
    assert not keeper.worth_reload(6 * GB, 6 * GB, 100)            # whole on the GPU already
    assert not keeper.worth_reload(6 * GB, int(1.3 * GB), 1000)    # 4.7 GB missing, 1 GB free: still busy
    assert keeper.worth_reload(6 * GB, int(1.3 * GB), 5500)        # the other program is gone
    assert keeper.worth_reload(6 * GB, 3 * GB, None)               # no nvidia-smi: try


def test_a_smaller_installed_model_when_memory_is_short():
    assert keeper.smaller_model(["qwen3:8b", "qwen3:1.7b", "bge-m3:latest"], "qwen3:8b") == "qwen3:1.7b"
    assert keeper.smaller_model(["qwen3:8b", "qwen3:4b", "qwen3:1.7b"], "qwen3:4b") == "qwen3:1.7b"
    assert keeper.smaller_model(["qwen3:8b"], "qwen3:8b") is None


class Refusing(Provider):
    name = "gemini"

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    async def stream(self, messages, *, web, max_tokens=None, models=None):
        self.calls += 1
        raise RegionError("Gemini недоступен из вашего региона — нужен VPN")
        yield ""


class Answering(Provider):
    name = "local"

    def available(self):
        return True

    async def stream(self, messages, *, web, max_tokens=None, models=None):
        yield "ответ"


def test_a_cloud_that_refuses_the_country_is_paused_and_reported_once():
    from assistant.config import load_settings

    hub = LlmHub(load_settings().llm)
    refusing = Refusing()
    hub.providers = {"gemini": refusing, "local": Answering()}
    told = []
    hub.on_unavailable = lambda name, why: told.append(why)

    async def ask():
        return await hub.complete(["gemini", "local"], [Msg("user", "вопрос")], web=False)
    assert asyncio.run(ask()) == "ответ"
    assert asyncio.run(ask()) == "ответ"
    assert refusing.calls == 1                       # paused for an hour after the first refusal
    assert len(told) == 1 and "VPN" in told[0]


def test_a_guessed_close_is_confirmed_first():
    from assistant.brain import Brain
    from assistant.skills.base import Intent, Reply, Skill
    from tests.test_skills import FakeApp

    done = []

    class Windows(Skill):
        name = "windows"

        async def handle(self, intent):
            done.append(intent.slots["name"])
            return Reply("Закрыл.")

    from assistant.brain import Dialog

    app = FakeApp()
    app.dialog = Dialog(600, 20)
    skill = Windows()
    skill.setup(app)
    brain = Brain(app, [skill])
    hit = (skill, Intent("windows", "close", {"name": "всё"}, "закрой всё"))

    async def run():
        asked = await brain._run_commands("спрячь всё", [hit], "router")
        assert done == [] and asked.confirm is not None and "да" in asked.speech
        await asked.confirm()
        assert done == ["всё"]
        await brain._run_commands("закрой телеграм", [hit], "skill")     # a module's own pattern: no question
        assert done == ["всё", "всё"]
    asyncio.run(run())
