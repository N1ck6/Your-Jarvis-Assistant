"""News: the main headlines, the news of a city or on a topic, from Google News RSS (no key, no tokens).

"новости", "что нового", "новости Казани", "что нового в Москве", "новости технологий", "открой вторую новость".
Three headlines are spoken, up to eight are shown on a card; the links open in the browser.
"""
from __future__ import annotations

import email.utils
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx

from assistant.core import Card, Deck
from assistant.nlu import to_nominative
from assistant.skills.base import Intent, Reply, Skill, Tool

log = logging.getLogger("news")

_TOPICS = {"технолог": "технологии", "спорт": "спорт", "экономик": "экономика", "финанс": "финансы", "наук": "наука",
           "игр": "видеоигры", "кино": "кино", "авто": "автомобили", "политик": "политика", "космос": "космос",
           "ит": "IT", "it": "IT", "крипт": "криптовалюта"}
_TRIGGER = re.compile(
    r"^(?:какие |последние |свежие |главные |самые важные |что за |расскажи |скажи |дай |покажи |прочитай )*"
    r"(?:новости|новостей|новость|сводку новостей|заголовки)(?:\s+(?:дня|за сегодня|сегодня|на сегодня|в мире|мира|"
    r"по миру|в стране))?(?:\s+(?P<rest>.+))?$|"
    r"^что (?:нового|новенького|случилось|происходит|слышно)(?:\s+(?:в мире|сегодня|за сегодня|в стране))?(?:\s+(?P<rest2>.+))?$|"
    r"^что (?:пишут|говорят) (?:в новостях|в мире)(?:\s+(?P<rest3>.+))?$")
_OPEN = re.compile(r"^(?:открой|покажи|прочитай|подробнее|подробнее про|расскажи подробнее про)\s+"
                   r"(?P<n>первую|вторую|третью|четвертую|пятую|шестую|седьмую|восьмую|последнюю)(?:\s+новость)?$")
_ORD = {"первую": 0, "вторую": 1, "третью": 2, "четвертую": 3, "пятую": 4, "шестую": 5, "седьмую": 6, "восьмую": 7}


@dataclass
class Headline:
    title: str
    source: str
    link: str
    published: float


def parse_rss(xml: bytes) -> list[Headline]:
    out: list[Headline] = []
    for item in ET.fromstring(xml).iter("item"):
        title = html.unescape(item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()  # Google News appends " - Источник"
        try:
            published = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            published = 0.0
        if title:
            out.append(Headline(title, source, (item.findtext("link") or "").strip(), published))
    return out


def ago(ts: float) -> str:
    minutes = int((time.time() - ts) // 60)
    if ts <= 0 or minutes < 0:
        return ""
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    return f"{hours} ч назад" if hours < 24 else f"{hours // 24} дн назад"


class NewsSkill(Skill):
    name = "news"
    title = "Новости"
    examples = [
        'новости (главное в мире)',
        'новости <город>',
        'новости <тема>',
        'открой вторую новость',
    ]

    def __init__(self) -> None:
        self.last: list[Headline] = []
        self._cache: dict[str, tuple[float, list[Headline]]] = {}

    def match(self, text: str) -> Intent | None:
        if (m := _OPEN.match(text)) and self.last:
            n = m.group("n")
            return Intent(self.name, "open", {"index": len(self.last) - 1 if n == "последнюю" else _ORD[n]}, text)
        m = _TRIGGER.match(text)
        if not m:
            return None
        rest = (m.group("rest") or m.group("rest2") or m.group("rest3") or "").strip()
        return Intent(self.name, "news", self._what(rest), text)

    @staticmethod
    def _what(rest: str) -> dict:
        """'в казани' / 'москвы' -> city, 'технологий' / 'про спорт' -> topic, '' -> the main news."""
        rest = re.sub(r"^(?:в|во|про|о|об|по|из|на тему|касательно)\s+", "", rest).strip()
        if not rest or rest in ("мира", "дня", "сегодня"):
            return {"city": "", "topic": ""}
        for stem, topic in _TOPICS.items():
            if rest.startswith(stem):
                return {"city": "", "topic": topic}
        return {"city": rest, "topic": ""}

    def followup(self, text: str, last: Intent) -> Intent | None:
        """"а в Казани?", "а спорта?" after the news."""
        m = re.match(r"^(?:а|и)\s+(.+?)\??$", text)
        if not m or len(m.group(1).split()) > 3:
            return None
        return Intent(self.name, "news", self._what(m.group(1)))

    def tools(self) -> list[Tool]:
        return [Tool("news", "Свежие новости: главное, новости города или темы.",
                     {"type": "object", "properties": {"city": {"type": "string"}, "topic": {"type": "string"}}},
                     "news")]

    async def _fetch(self, url: str) -> list[Headline]:
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < self.app.cfg.news.cache_min * 60:
            return cached[1]
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url)
            r.raise_for_status()
        items = parse_rss(r.content)
        self._cache[url] = (time.monotonic(), items)
        return items

    def _url(self, city: str, topic: str) -> str:
        cfg = self.app.cfg.news
        if city and city.lower() in cfg.feeds:
            return cfg.feeds[city.lower()]
        if not city and not topic:
            return cfg.feeds.get("главное") or "https://news.google.com/rss?hl=ru&gl=RU&ceid=RU:ru"
        query = f"{city or topic} when:1d"
        return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=ru&gl=RU&ceid=RU:ru"

    async def handle(self, intent: Intent) -> Reply:
        if intent.action == "open":
            index = int(intent.slots.get("index", 0))
            if not 0 <= index < len(self.last):
                return Reply("Такой новости в списке нет.")
            self.app.ui.open_url(self.last[index].link)
            return Reply("Открываю.", reaction="ok", listen_after=False)
        city = str(intent.slots.get("city") or "").strip()
        topic = str(intent.slots.get("topic") or "").strip()
        if city:
            city = to_nominative(city)
        try:
            items = await self._fetch(self._url(city, topic))
        except (httpx.HTTPError, ET.ParseError) as exc:
            log.warning("Новости: %s", exc)
            return Reply("Не удалось получить новости, источник не отвечает.")
        seen, fresh = set(), []
        for h in sorted(items, key=lambda h: -h.published):
            key = h.title.lower()[:60]
            if key not in seen:
                seen.add(key)
                fresh.append(h)
        fresh = fresh[:8]
        if not fresh:
            return Reply(f"Свежих новостей {'про ' + (city or topic) if city or topic else ''} не нашёл.")
        self.last = fresh
        where = f"Новости: {city}" if city else f"Новости: {topic}" if topic else "Главные новости"
        lines = [f"{i + 1}. {h.title} — {h.source}{', ' + ago(h.published) if ago(h.published) else ''}"
                 for i, h in enumerate(fresh)]
        self.app.ui.show_deck(Deck(title=where, cards=[Card("", "\n".join(lines), "")], done=True))
        n = self.app.cfg.news.spoken
        spoken = ". ".join(h.title.rstrip(".") for h in fresh[:n])
        head = f"Главное {'в городе ' + city if city else 'по теме ' + topic if topic else 'сейчас'}"
        return Reply(f"{head}: {spoken}. Остальное на экране.",
                     tool_result="\n".join(h.title for h in fresh))


def create() -> Skill:
    return NewsSkill()
