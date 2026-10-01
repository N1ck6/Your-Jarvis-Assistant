"""Long-term facts about the user: "запомни, что я не ем мясо". Facts go into every model prompt."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

from rapidfuzz import fuzz

from assistant.core import Card, Deck
from assistant.paths import DATA_DIR
from assistant.skills.base import Intent, Reply, Skill, Tool

log = logging.getLogger("memory")
FILE = DATA_DIR / "memory.json"

_REMEMBER = re.compile(r"^(запомни|запомнить|учти|имей в виду|знай|заруби себе на носу|прими к сведению|запомни на будущее)\s+(что\s+|то что\s+)?(?P<fact>.+)$")
_RAW_REMEMBER = re.compile(r"^\W*(запомни на будущее|запомни|запомнить|учти|имей в виду|знай|заруби себе на носу|прими к сведению)\W+((то )?что\W+)?", re.I)
_RECALL = re.compile(
    r"^что ты (обо мне|про меня|о мне) (знаешь|помнишь|запомнил)|^что ты (запомнил|помнишь)|^(покажи|открой) (свою )?память|"
    r"^что ты знаешь обо мне|^(мои|все) факты|^что ты обо мне запомнил")
_FORGET_ALL = re.compile(r"^(забудь|сотри|удали|очисти) (все|всё|всю память|память|все обо мне|всё обо мне|все что знаешь)( обо мне)?$")
_FORGET = re.compile(r"^(забудь|забыть|удали из памяти|сотри из памяти|больше не помни)\s+(что\s+|про\s+|о том что\s+)?(?P<fact>.+)$")


class MemorySkill(Skill):
    name = "memory"
    title = "Память о пользователе"
    examples = [
        'запомни что <факт>',
        'что ты обо мне знаешь',
        'забудь что <факт>',
    ]

    def __init__(self) -> None:
        self._facts: list[dict] = []

    async def start(self) -> None:
        if FILE.exists():
            try:
                self._facts = json.loads(FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("memory.json повреждён")

    def facts(self) -> list[str]:
        return [f["text"] for f in self._facts]

    def _save(self) -> None:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(self._facts, ensure_ascii=False, indent=1), encoding="utf-8")

    def match(self, text: str) -> Intent | None:
        if m := _REMEMBER.match(text):
            return Intent(self.name, "remember", {"fact": m.group("fact")}, text)
        if _RECALL.match(text):
            return Intent(self.name, "recall", text=text)
        if _FORGET_ALL.match(text):
            return Intent(self.name, "forget_all", text=text)
        if m := _FORGET.match(text):
            return Intent(self.name, "forget", {"fact": m.group("fact")}, text)
        return None

    def tools(self) -> list[Tool]:
        return [Tool("remember_fact", "Запомнить важный факт о пользователе надолго (предпочтения, данные, привычки).",
                     {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}, "remember")]

    async def handle(self, intent: Intent) -> Reply:
        if intent.action == "remember":
            raw = _RAW_REMEMBER.sub("", intent.raw).strip(" .") if intent.raw else str(intent.slots["fact"])
            fact = raw[:1].lower() + raw[1:]
            if any(fuzz.ratio(fact.lower(), f["text"].lower()) >= 90 for f in self._facts):
                return Reply("Это я уже знаю.", listen_after=False)
            self._facts.append({"text": fact, "added": dt.date.today().isoformat()})
            self._save()
            log.info("Запомнил: %s", fact)
            return Reply(f"Запомнил, {self.app.cfg.assistant.address}.", reaction="ok", tool_result="запомнено",
                         listen_after=False)
        if intent.action == "recall":
            facts = self.facts()
            if not facts:
                return Reply("Пока ничего. Скажите «запомни, что…», и я учту.")
            self.app.ui.show_deck(Deck(title="Что я о вас знаю", cards=[Card("", "\n".join(f"- {f}" for f in facts), "")],
                                       done=True))
            head = "; ".join(facts[:5])
            return Reply(f"Вот что я помню: {head}." + (f" И ещё {len(facts) - 5} на экране." if len(facts) > 5 else ""))
        if intent.action == "forget":
            spoken = str(intent.slots["fact"])
            best = max(self._facts, key=lambda f: fuzz.WRatio(spoken, f["text"].lower()), default=None)
            if best is None or fuzz.WRatio(spoken, best["text"].lower()) < 70:
                return Reply("Такого я не помню.")
            self._facts.remove(best)
            self._save()
            return Reply(f"Забыл: {best['text']}.", listen_after=False)
        if intent.action == "forget_all":
            if not self._facts:
                return Reply("Память и так пуста.")

            async def do() -> Reply:
                self._facts = []
                self._save()
                return Reply("Память очищена.", listen_after=False)

            return Reply(f"Стереть всё, что я о вас знаю, {len(self._facts)} фактов?", confirm=do)
        return Reply()


def create() -> Skill:
    return MemorySkill()
