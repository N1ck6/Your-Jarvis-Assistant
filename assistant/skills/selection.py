"""Actions on the text selected in any window: translate, explain, retell, fix, rewrite, read aloud.

The selection is copied with Ctrl+C, the clipboard is restored afterwards. Everything runs on the local model.
"""
from __future__ import annotations

import logging
import re

from assistant import winutil
from assistant.core import Card, Deck
from assistant.llm.providers import Msg
from assistant.skills.base import Intent, Reply, Skill, Tool, run_blocking

log = logging.getLogger("selection")

_SEL = r"(?:это|эту|этот|вот это|выделенное|выделенный текст|выделенный фрагмент|выделение|что выделено|что я выделил|текст|этот текст|фрагмент|абзац)"
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("fix", re.compile(rf"^(исправь|исправить|поправь)( (ошибки|опечатки|орфографию|грамматику|текст|пунктуацию))?( в)?( {_SEL})?$|"
                       rf"^проверь (ошибки|опечатки|орфографию|грамматику|пунктуацию)( в)?( {_SEL})?$")),
    ("rewrite", re.compile(rf"^(перепиши|переформулируй|сформулируй|перефразируй)( {_SEL})?( (официально|вежливо|короче|проще|понятнее|красивее|деловым стилем|по-деловому|дружелюбнее|формально|неформально))?$")),
    ("translate", re.compile(rf"^(переведи|перевести|переводи|сделай перевод)( {_SEL})?( на (английский|русский|немецкий|французский|испанский|китайский|японский|украинский|английском|русском))?$")),
    ("summary", re.compile(rf"^(перескажи|сократи|кратко|суммаризируй|сделай выжимку|выжимку|резюмируй|о чем (это|тут|этот текст|текст|статья)|в двух словах)( {_SEL})?( кратко| коротко| в двух словах)?$")),
    ("read", re.compile(rf"^(прочитай|прочти|зачитай|озвучь|прочитай вслух|читай)( мне)?( {_SEL})?( вслух)?$")),
    ("explain", re.compile(rf"^(объясни|поясни|растолкуй|разъясни|что (это|тут) (значит|означает)|что значит {_SEL}|что это)( мне)?( {_SEL})?$")),
]
_LANGS = {"английск": "английский", "русск": "русский", "немецк": "немецкий", "французск": "французский",
          "испанск": "испанский", "китайск": "китайский", "японск": "японский", "украинск": "украинский"}

PROMPTS = {
    "translate": "Переведи текст на {lang} язык. Выведи только перевод, без пояснений и кавычек.",
    "fix": "Исправь орфографию, пунктуацию и грамматику. Сохрани язык, смысл, стиль и форматирование. Выведи только исправленный текст.",
    "rewrite": "Перепиши текст{style}. Сохрани язык и смысл. Выведи только новый текст.",
    "summary": "Перескажи суть текста по-русски в 2–3 коротких предложениях для голоса. Без вступлений.",
    "explain": "Объясни по-русски простыми словами, что означает этот текст и в чём суть: 2–4 коротких предложения для голоса.",
}


class SelectionSkill(Skill):
    name = "selection"
    title = "Работа с выделенным текстом"

    def match(self, text: str) -> Intent | None:
        for action, pattern in _PATTERNS:
            m = pattern.match(text)
            if m:
                return Intent(self.name, action, {"lang": _target_lang(text), "style": _style(text)}, text)
        return None

    def tools(self) -> list[Tool]:
        return [Tool("selected_text", "Действие с текстом, выделенным пользователем в любом окне.",
                     {"type": "object", "properties": {"action": {"type": "string", "enum": [
                         "translate", "explain", "summary", "fix", "rewrite", "read"]}}, "required": ["action"]},
                     "tool")]

    async def handle(self, intent: Intent) -> Reply:
        action = intent.slots.get("action") if intent.action == "tool" else intent.action
        text = await run_blocking(winutil.copy_selection)
        if not text or not text.strip():
            return Reply("Сначала выделите текст, а потом скажите, что с ним сделать.")
        text = text.strip()[:6000]
        log.info("Выделено %d символов, действие %s", len(text), action)
        if action == "read":
            return Reply(stream=_once(text))

        lang = intent.slots.get("lang") or ("английский" if _is_cyrillic(text) else "русский")
        style = intent.slots.get("style") or ""
        system = PROMPTS[action].format(lang=lang, style=f" ({style})" if style else "")
        messages = [Msg("system", system), Msg("user", text)]

        if action in ("summary", "explain"):
            return Reply(stream=self.app.llm.stream(["local"], messages, web=False, max_tokens=400))

        result = (await self.app.llm.local.complete(messages, max_tokens=2000)).strip()
        if not result:
            return Reply("Не получилось обработать текст.")
        if action in ("fix", "rewrite"):
            await run_blocking(winutil.paste_text, result)  # the selection is still active: replaced in place
            return Reply("Готово.", reaction="ok", listen_after=False)
        # translate
        await run_blocking(winutil.set_clipboard_text, result)
        self.app.ui.show_deck(Deck(title=f"Перевод · {lang}", cards=[Card("", result, "")], done=True))
        if lang == "русский" and len(result) < 400:
            return Reply(result, listen_after=False)
        return Reply("Перевод на экране и в буфере обмена.", listen_after=False)


async def _once(text: str):
    yield text


def _is_cyrillic(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("а" <= c.lower() <= "я" or c in "ёЁ" for c in letters) / len(letters) > 0.5


def _target_lang(text: str) -> str:
    for stem, lang in _LANGS.items():
        if f"на {stem}" in text:
            return lang
    return ""


def _style(text: str) -> str:
    m = re.search(r"(официально|вежливо|короче|проще|понятнее|красивее|деловым стилем|по-деловому|дружелюбнее|формально|неформально)$", text)
    return m.group(1) if m else ""


def create() -> Skill:
    return SelectionSkill()
