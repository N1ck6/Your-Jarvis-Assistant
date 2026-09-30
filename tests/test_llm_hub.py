import asyncio

from assistant.config import LlmCfg
from assistant.llm.hub import FAIL_TEXT, OFFLINE_NOTE, LlmHub
from assistant.llm.providers import AuthError, Msg, Provider, ProviderError, QuotaError


class Fake(Provider):
    def __init__(self, name, error=None, text="ok", fail_after_first=False):
        self.name = name
        self.error = error
        self.text = text
        self.fail_after_first = fail_after_first
        self.calls = 0
        self.seen: list[Msg] = []

    def available(self):
        return True

    async def stream(self, messages, *, web, max_tokens=None):
        self.calls += 1
        self.seen = messages
        if self.error and not self.fail_after_first:
            raise self.error
        yield self.text
        if self.fail_after_first:
            raise ProviderError("broke mid-stream")


def make_hub(**providers):
    hub = LlmHub(LlmCfg())
    hub.providers = providers
    if "local" in providers:
        hub.local = providers["local"]
    return hub


def collect(hub, chain, web=True):
    async def run():
        return "".join([d async for d in hub.stream(chain, [Msg("user", "q")], web=web)])
    return asyncio.run(run())


def test_first_provider_answers():
    hub = make_hub(gemini=Fake("gemini", text="A"), local=Fake("local", text="L"))
    assert collect(hub, ["gemini", "local"]) == "A"
    assert hub.last_provider == "gemini"


def test_quota_puts_on_cooldown_and_falls_back():
    g = Fake("gemini", error=QuotaError("429"))
    hub = make_hub(gemini=g, groq=Fake("groq", text="G"))
    assert collect(hub, ["gemini", "groq"]) == "G"
    assert collect(hub, ["gemini", "groq"]) == "G"
    assert g.calls == 1  # skipped while cooling down
    assert "пауза" in hub.status()["gemini"]


def test_auth_error_disables_provider():
    g = Fake("gemini", error=AuthError("bad key"))
    hub = make_hub(gemini=g, local=Fake("local", text="L"))
    collect(hub, ["gemini", "local"])
    collect(hub, ["gemini", "local"])
    assert g.calls == 1


def test_local_fallback_gets_offline_note_for_web_questions():
    local = Fake("local", text="L")
    hub = make_hub(gemini=Fake("gemini", error=ProviderError("down")), local=local)
    collect(hub, ["gemini", "local"], web=True)
    assert local.seen[0].content == OFFLINE_NOTE
    collect(hub, ["local"], web=False)
    assert local.seen[0].content == "q"


def test_mid_stream_failure_keeps_partial_answer():
    hub = make_hub(gemini=Fake("gemini", text="Начало", fail_after_first=True), local=Fake("local", text="L"))
    assert collect(hub, ["gemini", "local"]) == "Начало"


def test_all_fail():
    hub = make_hub(gemini=Fake("gemini", error=ProviderError("x")))
    assert collect(hub, ["gemini"]) == FAIL_TEXT
