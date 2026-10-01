"""Explanation window: text cards on screen, the voice explains them in other words.

The model streams cards in a simple line format so the first card is shown and
spoken after ~150 tokens instead of waiting for the whole deck. Cards are meant to be useful
on their own (a cheat sheet: definitions, steps, numbers, examples); the voice adds an analogy.
Paging by hand speaks the chosen card; closing the window (Esc, ✕) stops the narration.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncIterator

from assistant.core import Card, Deck
from assistant.llm.providers import Msg
from assistant.log import private
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

PROMPT = """Ты готовишь наглядное объяснение для окна с карточками. Пользователь ЧИТАЕТ карточку, а голос в это время
коротко поясняет её простыми словами: аналогия, пример из жизни, зачем это нужно. Голос не зачитывает карточку.

Карточки должны быть полезны сами по себе, как хорошая шпаргалка: определения, конкретные факты, числа, шаги,
названия, примеры, команды или формулы. Никакой воды и общих фраз вроде «это важная тема».

Формат строго такой, без markdown:
ЗАГОЛОВОК: <тема, 2–6 слов>
###
КАРТОЧКА: <заголовок карточки, 2–5 слов>
ЭКРАН:
- <законченная мысль с конкретикой, 6–20 слов>
- <Термин — короткое пояснение>
1. <шаг процесса; для последовательностей нумеруй строки вместо «-»>
> <одна строка кода, команды, формулы или примера запроса — только если правда помогает>
ГОЛОС: <1–2 разговорных предложения, до 35 слов>
###
КАРТОЧКА: ...

Пример хорошей карточки (на другую тему):
КАРТОЧКА: Как работает DNS
ЭКРАН:
1. Браузер спрашивает у резолвера провайдера IP-адрес для example.com
2. Резолвер идёт к корневому серверу, затем к серверу зоны .com
3. Авторитетный сервер домена отвечает: 93.184.216.34
4. Ответ кэшируется на время TTL, например 3600 секунд
> nslookup example.com
ГОЛОС: Это как справочная: вы называете имя, вам диктуют номер, и вы его записываете, чтобы не звонить снова.

Сделай от 4 до {max_cards} карточек: 1) суть и точное определение; 2) как устроено или работает, по шагам;
3) конкретный пример с числами, кодом или реальной ситуацией; 4) где применяют, плюсы и минусы или частые ошибки;
последняя карточка — итог из 3 главных тезисов. На каждой карточке 3–5 строк ЭКРАН, и каждая строка сообщает
новый конкретный факт: название, число, команду, шаг или пример, а не пересказ заголовка.
Пиши по-русски; общепринятые английские термины — латиницей (REST API, HTTP, GPU). Тема: {topic}"""

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


class Narration:
    """A deck being explained: which card to speak next, user paging and closing."""

    def __init__(self) -> None:
        self.deck: Deck | None = None
        self.jump: int | None = None
        self.closed = False
        self.more = asyncio.Event()   # a new card arrived or the user did something


class ExplainSkill(Skill):
    name = "explain"
    title = "Объяснение в окне"
    examples = [
        'объясни подробно <тема> (окно с карточками)',
    ]

    def __init__(self) -> None:
        self.narration: Narration | None = None
        self.last_deck: Deck | None = None

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

    # ------------------------------------------------------------------ window events (from the UI)
    def user_paged(self, index: int) -> None:
        """The user flipped to card `index` by hand: speak that card (now or as the next one)."""
        n = self.narration
        if n is not None and not n.closed:
            n.jump = index
            n.more.set()
            self.app.speaker.stop()   # the narration loop continues from the chosen card
            return
        deck = self.last_deck
        if deck and 0 <= index < len(deck.cards) and deck.cards[index].speech:
            self.app.speaker.stop()
            asyncio.create_task(self.app.speaker.say(deck.cards[index].speech))

    def user_closed(self) -> None:
        n = self.narration
        if n is not None:
            n.closed = True
            n.more.set()
        self.app.speaker.stop()

    # ------------------------------------------------------------------ generation
    async def _generate(self, topic: str) -> AsyncIterator[str]:
        cfg = self.app.cfg.explain
        messages = [Msg("user", PROMPT.format(topic=topic, max_cards=max(4, cfg.max_cards)))]
        hub = self.app.llm
        # Cloud: Groq/Gemini without web search (~2.5k tokens a deck); local: free, ~25 s for a deck.
        chain = [p for p in self.app.cfg.llm.info_chain if p != "local"] + ["local"] if cfg.provider == "cloud" else ["local"]
        async for delta in hub.stream(chain, messages, web=False, max_tokens=1800):
            yield delta

    async def handle(self, intent: Intent) -> Reply:
        topic = str(intent.slots.get("topic") or "").strip()
        if not topic:
            topic = self.app.dialog.last_topic()
        if not topic:
            return Reply("Что объяснить?")
        app = self.app
        log.info("Объяснение: %s", private(topic))
        narration = Narration()
        self.narration = narration
        produced = asyncio.Event()

        async def produce() -> None:
            text = ""
            try:
                async for delta in self._generate(topic):
                    text += delta
                    deck = parse_deck(text, final=False)
                    if deck.title and narration.deck is None:
                        narration.deck = deck
                        app.ui.show_deck(deck)
                    if narration.deck is not None and len(deck.cards) > len(narration.deck.cards):
                        narration.deck.cards = deck.cards
                        app.ui.update_deck(narration.deck)
                        narration.more.set()
                deck = parse_deck(text, final=True)
                if not deck.title:
                    deck.title = topic[:1].upper() + topic[1:]
                if narration.deck is None:
                    app.ui.show_deck(deck)
                narration.deck = deck
                app.ui.update_deck(deck)
                if not deck.cards:
                    log.warning("Модель не вернула карточек:\n%s", private(text[:500]))
            finally:
                produced.set()
                narration.more.set()

        producer = asyncio.create_task(produce())
        spoken_any = False
        try:
            await app.speaker.say("Сейчас покажу.")
            index = 0
            while not narration.closed:
                cards = narration.deck.cards if narration.deck else []
                if index < len(cards):
                    narration.jump = None
                    app.ui.show_card(index)
                    await app.speaker.say(cards[index].speech)
                    spoken_any = True
                    if narration.closed:
                        break
                    index = narration.jump if narration.jump is not None else index + 1
                    continue
                if produced.is_set():
                    break
                narration.more.clear()
                await narration.more.wait()
                if narration.jump is not None:
                    index = narration.jump
        finally:
            if not producer.done():
                producer.cancel()
            self.narration = None
            self.last_deck = narration.deck
        if narration.closed:
            return Reply(spoken=True, listen_after=False)
        if not spoken_any:
            return Reply("Не получилось подготовить объяснение.")
        app.dialog.add("assistant", f"[показал объяснение: {topic}]")
        return Reply(spoken=True)


def create() -> Skill:
    return ExplainSkill()
