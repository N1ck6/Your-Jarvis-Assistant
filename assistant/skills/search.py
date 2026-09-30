"""Search in the browser: "найди в яндексе ...", "найди в браузере ...", "найди на ютубе ..."."""
from __future__ import annotations

import re
import webbrowser
from urllib.parse import quote_plus

from assistant.skills.base import Intent, Reply, Skill, Tool, run_blocking

_VERB = r"(?:найди|найти|поищи|поискать|ищи|поиск|загугли|погугли|пробей|посмотри|глянь|открой поиск|вбей)"
_WHERE = (r"(?P<where>яндексе|яндекс|гугле|гугл|google|браузере|браузер|интернете|сети|инете|ютубе|ютуб|youtube|"
          r"википедии|вики|картах|карте|маркете|яндекс маркете)")
_FORWARD = re.compile(rf"^{_VERB}\s+(?:в|на|через|по)\s+{_WHERE}\s+(?:про\s+|о\s+|об\s+)?(?P<q>.+)$")
_BACKWARD = re.compile(rf"^{_VERB}\s+(?P<q>.+?)\s+(?:в|на|через)\s+{_WHERE}$")
_BARE = re.compile(r"^(?P<verb>загугли|погугли|гугл|яндекс)\s+(?P<q>.+)$")

_ENGINE = {"гугл": "google", "google": "google", "ютуб": "youtube", "youtube": "youtube", "вики": "wiki",
           "карт": "maps", "маркет": "market"}


def _engine(where: str, default: str) -> str:
    for stem, engine in _ENGINE.items():
        if where.startswith(stem) or stem in where:
            return engine
    return default  # яндекс / браузер / интернет


class SearchSkill(Skill):
    name = "search"
    title = "Поиск в браузере"

    def match(self, text: str) -> Intent | None:
        default = self.app.cfg.search.default
        for pattern in (_FORWARD, _BACKWARD):
            m = pattern.match(text)
            if m:
                return Intent(self.name, "search", {"q": m.group("q"), "engine": _engine(m.group("where"), default)}, text)
        m = _BARE.match(text)
        if m:
            engine = "google" if m.group("verb") in ("загугли", "погугли", "гугл") else "yandex"
            return Intent(self.name, "search", {"q": m.group("q"), "engine": engine}, text)
        return None

    def tools(self) -> list[Tool]:
        return [Tool("browser_search", "Открыть поиск в браузере (Яндекс, Google, YouTube).",
                     {"type": "object", "properties": {"q": {"type": "string"},
                                                       "engine": {"type": "string", "enum": ["yandex", "google", "youtube"]}},
                      "required": ["q"]}, "search")]

    async def handle(self, intent: Intent) -> Reply:
        q = str(intent.slots.get("q") or "").strip()
        if not q:
            return Reply("Что искать?")
        engines = self.app.cfg.search.engines
        engine = intent.slots.get("engine") or self.app.cfg.search.default
        url = engines.get(engine, engines.get("yandex", "https://yandex.ru/search/?text={q}")).format(q=quote_plus(q))
        await run_blocking(webbrowser.open, url)
        return Reply(f"Ищу: {q}.", reaction="ok", tool_result="поиск открыт", listen_after=False)


def create() -> Skill:
    return SearchSkill()
