"""Weather via Open-Meteo (free, no key)."""
from __future__ import annotations

import datetime as dt
import logging
import re

import httpx

from assistant.nlu import in_place, plural, to_nominative
from assistant.skills.base import Intent, Reply, Skill, Tool

log = logging.getLogger("weather")

_TRIGGER = re.compile(
    r"погод|температур|градус|прогноз|синоптик|(будет ли|пойдет ли|ожидается ли|обещают ли|собирается ли) (дождь|снег|гроза|ливень|град)|"
    r"(нужен ли|брать ли|взять ли) зонт|(холодно|тепло|жарко|морозно|ветрено|сыро|скользко) ли|что (сейчас )?на улице|что за окном|"
    r"как (там )?на улице|как одеться|что (мне )?надеть|во что одеться|(какой|сильный ли) ветер|мороз|сколько (сейчас )?на улице")
_CITY = re.compile(r"\b(?:в|во)\s+(?P<city>[а-яё-]+(?:\s+[а-яё-]+){0,2})")
_DAY_WORDS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
_STOP_AFTER_CITY = {"сегодня", "завтра", "послезавтра", "сейчас", "утром", "днём", "днем", "вечером", "ночью", "улице",
                    "будет", "на", "какая", "какой", "градусов", "погода"}

WMO = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь и туман", 51: "лёгкая морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "ледяная морось", 61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "ледяной дождь", 71: "небольшой снег", 73: "снег", 75: "сильный снег",
    77: "снежная крупа", 80: "ливень", 81: "ливни", 82: "сильные ливни", 85: "снегопад", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 99: "гроза с сильным градом",
}


def _deg(t: float) -> str:
    n = round(t)
    sign = "плюс " if n > 0 else "минус " if n < 0 else ""
    return f"{sign}{abs(n)} {plural(n, 'градус', 'градуса', 'градусов')}"


def _range(lo: float, hi: float) -> str:
    """'от плюс 5 до плюс 12 градусов' instead of repeating the unit."""
    a, b = round(lo), round(hi)
    if a == b:
        return _deg(a)
    sign = lambda n: "плюс " if n > 0 else "минус " if n < 0 else ""
    return f"от {sign(a)}{abs(a)} до {sign(b)}{abs(b)} {plural(b, 'градуса', 'градусов', 'градусов')}"


class WeatherSkill(Skill):
    name = "weather"
    title = "Погода"
    examples = [
        'какая погода',
        'погода в <город>',
        'погода завтра в <город>',
    ]

    def __init__(self) -> None:
        self._geo_cache: dict[str, tuple[float, float, str]] = {}

    def match(self, text: str) -> Intent | None:
        if not _TRIGGER.search(text):
            return None
        words = set(re.findall(r"[а-яё]+", text))
        day = next((d for w, d in _DAY_WORDS.items() if w in words), 0)
        city = ""
        m = _CITY.search(text)
        if m:
            words = []
            for w in m.group("city").split():
                if w in _STOP_AFTER_CITY:
                    break
                words.append(w)
            city = " ".join(words)
        return Intent(self.name, "forecast", {"city": city, "day": day}, text)

    def followup(self, text: str, last: Intent) -> Intent | None:
        """"а завтра?", "а послезавтра", "а в Сочи?", "а в Казани завтра" after a weather answer."""
        words = text.split()
        days = [_DAY_WORDS[w] for w in words if w in _DAY_WORDS]
        m = _CITY.search(text)
        city = ""
        if m:
            city = " ".join(w for w in m.group("city").split() if w not in _STOP_AFTER_CITY)
        rest = [w for w in words if w not in _DAY_WORDS and w not in ("а", "и", "там", "как", "что", "насчет", "по", "погода")]
        if city:
            rest = [w for w in rest if w not in ("в", "во") and w not in city.split()]
        if not (days or city) or rest:
            return None
        slots = dict(last.slots)
        if days:
            slots["day"] = days[0]
        if city:
            slots["city"] = city
        return Intent(self.name, "forecast", slots)

    def tools(self) -> list[Tool]:
        return [Tool("get_weather", "Погода и прогноз для города.",
                     {"type": "object", "properties": {
                         "city": {"type": "string", "description": "Город в именительном падеже, пусто = город по умолчанию"},
                         "day": {"type": "integer", "description": "0 — сегодня, 1 — завтра, 2 — послезавтра"}}},
                     "forecast")]

    async def _geocode(self, client: httpx.AsyncClient, city: str) -> tuple[float, float, str] | None:
        key = city.lower()
        if key in self._geo_cache:
            return self._geo_cache[key]
        for name in dict.fromkeys([to_nominative(city), city]):
            r = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                                 params={"name": name, "count": 1, "language": "ru", "format": "json"})
            r.raise_for_status()
            results = r.json().get("results") or []
            if results:
                g = results[0]
                self._geo_cache[key] = (g["latitude"], g["longitude"], g["name"])
                return self._geo_cache[key]
        return None

    async def handle(self, intent: Intent) -> Reply:
        city = str(intent.slots.get("city") or "").strip() or self.app.cfg.assistant.default_city
        day = max(0, min(int(intent.slots.get("day") or 0), 6))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                geo = await self._geocode(client, city)
                if not geo:
                    return Reply(f"Не нашёл город {city}.")
                lat, lon, place = geo
                r = await client.get("https://api.open-meteo.com/v1/forecast", params={
                    "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": day + 1,
                    "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
                    "wind_speed_unit": "ms",
                })
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as exc:
            log.warning("Open-Meteo: %s", exc)
            return Reply("Сервис погоды сейчас не отвечает.")

        daily = data["daily"]
        tmax, tmin = daily["temperature_2m_max"][day], daily["temperature_2m_min"][day]
        desc = WMO.get(daily["weather_code"][day], "")
        rain = daily.get("precipitation_probability_max", [None] * (day + 1))[day]
        rain_txt = f" Вероятность осадков {rain} {plural(rain, 'процент', 'процента', 'процентов')}." if rain and rain >= 30 else ""
        if day == 0:
            cur = data["current"]
            wind = round(cur["wind_speed_10m"])
            text = (f"Сейчас {in_place(place)} {_deg(cur['temperature_2m'])}, {WMO.get(cur['weather_code'], '')}, "
                    f"ветер {wind} {plural(wind, 'метр', 'метра', 'метров')} в секунду. "
                    f"Днём до {_deg(tmax)}, ночью до {_deg(tmin)}.{rain_txt}")
        else:
            when = "Завтра" if day == 1 else "Послезавтра" if day == 2 else (dt.date.today() + dt.timedelta(days=day)).strftime("%d.%m")
            text = f"{when} {in_place(place)} {_range(tmin, tmax)}, {desc}.{rain_txt}"
        return Reply(text)


def create() -> Skill:
    return WeatherSkill()
