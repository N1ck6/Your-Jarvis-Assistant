"""Actions on the selected text: a prompt skill (commands.toml, prompts/). The code adds what a template cannot:
the target language and the style from the phrase, and the tool for the local model."""
from __future__ import annotations

import re
from typing import Any

from assistant.skills.base import Intent, Reply, Skill, Tool

_LANGS = {"английск": "английский", "русск": "русский", "немецк": "немецкий", "французск": "французский",
          "испанск": "испанский", "китайск": "китайский", "японск": "японский", "украинск": "украинский"}
_STYLE = re.compile(r"(официально|вежливо|короче|проще|понятнее|красивее|деловым стилем|по-деловому|дружелюбнее|"
                    r"формально|неформально)$")


class SelectionSkill(Skill):
    def tools(self) -> list[Tool]:
        return [Tool("selected_text", "Действие с текстом, выделенным пользователем в любом окне.",
                     {"type": "object", "properties": {"action": {"type": "string", "enum": [
                         "translate", "explain", "summary", "fix", "rewrite", "read"]}}, "required": ["action"]},
                     "tool")]

    async def handle(self, intent: Intent) -> Reply:
        if intent.action == "tool":  # the local model asked for it
            intent = Intent(self.name, str(intent.slots.get("action", "explain")), {}, intent.text, intent.raw)
        return await super().handle(intent)

    def prepare(self, intent: Intent, text: str) -> dict[str, Any]:
        lang = _target_lang(intent.text) or ("английский" if _is_cyrillic(text) else "русский")
        m = _STYLE.search(intent.text)
        return {"lang": lang, "to_russian": lang == "русский", "style": f" ({m.group(1)})" if m else ""}


def _is_cyrillic(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("а" <= c.lower() <= "я" or c in "ёЁ" for c in letters) / len(letters) > 0.5


def _target_lang(text: str) -> str:
    for stem, lang in _LANGS.items():
        if f"на {stem}" in text:
            return lang
    return ""


def create() -> Skill:
    return SelectionSkill()
