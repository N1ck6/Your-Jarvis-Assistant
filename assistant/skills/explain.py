"""Explanation window: text cards on screen, the voice explains them in other words.

The model streams cards in a simple line format so the first card is shown and
spoken after ~100 tokens instead of waiting for the whole deck.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncIterator

from assistant.core import Card, Deck
from assistant.llm.providers import Msg
from assistant.skills.base import Intent, Reply, Skill, Tool

log = logging.getLogger("explain")

_HOW = r"(?:подробно|подробнее|детально|на экране|в окне|с окном|наглядно|по полочкам|на пальцах|с картинками|по шагам|пошагово|в деталях|развернуто)"
_TRIGGERS = [
    re.compile(rf"^(?:объясни|расскажи|поясни|разъясни|растолкуй|опиши|разбери|покажи)\s+(?:мне\s+)?{_HOW}\s*(?:что такое|что это|про|о|об|как|почему|зачем|тему)?\s*(?P<topic>.*)$"),
    re.compile(rf"^(?:объясни|расскажи|разбери)\s+(?:мне\s+)?(?:что такое|про|о|об|как работает|как устроен\w*)?\s*(?P<topic>.+?)\s+{_HOW}$"),
    re.compile(r"^(?:сделай|покажи|подготовь|открой|составь|сгенерируй|собери)\s+(?:мне\s+)?(?:презентацию|объяснение|разбор|окно|слайды|карточки|лекцию|конспект|шпаргалку)\s*(?:про|о|об|на тему|по теме|по|как)?\s*(?P<topic>.*)$"),
    re.compile(r"^(?:подробнее|расскажи подробнее|а подробнее|давай подробнее|можно подробнее|поподробнее|расскажи поподробнее|детальнее)\s*(?:про|о|об)?\s*(?P<topic>.*)$"),
    re.compile(r"^(?:разбери|разберем|разобрать)\s+(?:тему|по полочкам)\s+(?P<topic>.+)$"),
]

PROMPT = """Ты готовишь короткое наглядное объяснение для окна на экране. Пользователь читает текст на карточке,
а голос одновременно поясняет его ПРОСТЫМИ словами. Голос не зачитывает карточку, а дополняет: аналогия, пример, зачем это нужно.

Формат ответа строго такой, без markdown кроме «- » в начале строк:
ЗАГОЛОВОК: <тема, 2–6 слов>
###
КАРТОЧКА: <заголовок карточки>
ЭКРАН:
- <тезис, до 8 слов>
- <тезис, до 8 слов>
ГОЛОС: <1–2 коротких разговорных предложения, до 30 слов>
###
КАРТОЧКА: ...

Сделай от 3 до {max_cards} карточек: сначала суть, потом как работает, потом пример, в конце вывод.
Пиши по-русски. Числа можно цифрами. Тема: {topic}"""

_CARD_SPLIT = re.compile(r"^###\s*$", re.M)


def parse_deck(text: str, final: bool) -> Deck:
    """Parses what has streamed so far. The last card counts only when complete or the stream ended."""
    # While streaming, the title line counts only once it is complete.
    title_m = re.search(r"ЗАГОЛОВОК:\s*(.+)" + ("" if final else r"\n"), text, re.I)
    deck = Deck(title=title_m.group(1).strip() if title_m else "", done=final)
    blocks = _CARD_SPLIT.split(text)[1:]
    for i, block in enumerate(blocks):
        is_last = i == len(blocks) - 1
        if is_last and not final:
            break
        # Small models sometimes break marker case ("ГОЛос:"), so markers are case-insensitive.
        head = re.search(r"КАРТОЧКА:\s*(.+)", block, re.I)
        screen = re.search(r"ЭКРАН:[ \t]*\n?(.*?)(?=\n\s*ГОЛОС:|\Z)", block, re.S | re.I)
        speech = re.search(r"ГОЛОС:\s*(.+)", block, re.S | re.I)
        if not (head and speech):
            continue
        deck.cards.append(Card(head.group(1).strip(), (screen.group(1).strip() if screen else ""),
                               speech.group(1).strip()))
    return deck


class ExplainSkill(Skill):
    name = "explain"
    title = "Объяснение в окне"

    def match(self, text: str) -> Intent | None:
        for pattern in _TRIGGERS:
            m = pattern.match(text.strip(" .!?"))
            if m:
                topic = m.group("topic").strip(" .!?")
                return Intent(self.name, "explain", {"topic": topic}, text)
        return None

    def tools(self) -> list[Tool]:
        return [Tool("explain_topic", "Подробно объяснить тему наглядно: окно с карточками и голосовые пояснения. "
                     "Используй, когда пользователь просит подробно объяснить или показать.",
                     {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
                     "explain", exclusive=True)]

    async def _generate(self, topic: str) -> AsyncIterator[str]:
        cfg = self.app.cfg.explain
        messages = [Msg("user", PROMPT.format(topic=topic, max_cards=cfg.max_cards))]
        hub = self.app.llm
        chain = self.app.cfg.llm.info_chain if cfg.provider == "cloud" else ["local"]
        async for delta in hub.stream(chain, messages, web=cfg.provider == "cloud", max_tokens=900):
            yield delta

    async def handle(self, intent: Intent) -> Reply:
        topic = str(intent.slots.get("topic") or "").strip()
        if not topic:
            topic = self.app.dialog.last_topic()
        if not topic:
            return Reply("Что объяснить?")
        app = self.app
        log.info("Объяснение: %s", topic)

        cards_q: asyncio.Queue[Card | None] = asyncio.Queue()
        deck_box: dict[str, Deck] = {}

        async def produce() -> None:
            text = ""
            sent = 0
            try:
                async for delta in self._generate(topic):
                    text += delta
                    deck = parse_deck(text, final=False)
                    if deck.title and "deck" not in deck_box:
                        deck_box["deck"] = deck
                        app.ui.show_deck(deck)
                    for card in deck.cards[sent:]:
                        await cards_q.put(card)
                    sent = len(deck.cards)
                deck = parse_deck(text, final=True)
                if not deck.title:
                    deck.title = topic[:1].upper() + topic[1:]
                if "deck" not in deck_box:
                    deck_box["deck"] = deck
                    app.ui.show_deck(deck)
                for card in deck.cards[sent:]:
                    await cards_q.put(card)
                deck_box["deck"] = deck
                app.ui.update_deck(deck)
                if not deck.cards:
                    log.warning("Модель не вернула карточек:\n%s", text[:500])
            finally:
                await cards_q.put(None)

        producer = asyncio.create_task(produce())
        spoken_any = False
        try:
            await app.speaker.say("Сейчас покажу.")
            index = 0
            while True:
                card = await cards_q.get()
                if card is None:
                    break
                deck = deck_box.get("deck")
                if deck is not None and index >= len(deck.cards):
                    deck.cards.append(card)
                    app.ui.update_deck(deck)
                app.ui.show_card(index)
                await app.speaker.say(card.speech)
                spoken_any = True
                index += 1
        finally:
            if not producer.done():
                producer.cancel()
        if not spoken_any:
            return Reply("Не получилось подготовить объяснение.")
        app.dialog.add("assistant", f"[показал объяснение: {topic}]")
        return Reply(spoken=True)


def create() -> Skill:
    return ExplainSkill()
