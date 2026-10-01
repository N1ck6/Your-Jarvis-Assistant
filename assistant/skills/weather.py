"""Weather via Open-Meteo (free, no key)."""
from __future__ import annotations

import datetime as dt
import logging
import re

import httpx

from assistant.llm.providers import Msg
from assistant.nlu import in_place, plural, to_nominative
from assistant.skills.base import Intent, Reply, Skill, Tool

log = logging.getLogger("weather")

_TRIGGER = re.compile(
    r"погод|температур|градус|прогноз|синоптик|(будет ли|пойдет ли|ожидается ли|обещают ли|собирается ли) (дождь|снег|гроза|ливень|град)|"
    r"(нужен ли|брать ли|взять ли) зонт|(холодно|тепло|жарко|морозно|ветрено|сыро|скользко) ли|что (сейчас )?на улице|что за окном|"
    r"как (там )?на улице|как одеться|что (мне )?надеть|во что одеться|(какой|сильный ли) ветер|мороз|сколько (сейчас )?на улице")
_PRECIP = r"(?:дожд\w*|снег\w*|снегопад\w*|гроз\w*|ливн\w*|ливен\w*|осадк\w*|морос\w*|град)"
# Questions about the forecast rather than "what is the weather": when, which day, the whole week.
_TRIGGER_ASK = re.compile(
    rf"\b(когда|в какой день|какого числа|во сколько)\b.*\b({_PRECIP}|потеплеет|похолодает|потепление|похолодание|"
    r"тепло|теплее|холодно|холоднее|жарче|морозы?|заморозк\w*|солнц\w*|солнечно|жара|ясно)\b|"
    rf"\b{_PRECIP}\b.*\b(на (этой|следующей) неделе|на неделе|в выходные|на выходных|до конца недели)\b|"
    r"\b(потеплеет|похолодает|выпадет снег|пойдет снег|пойдет дождь|закончится дождь|кончится дождь|"
    r"прекратится дождь|перестанет дождь)\b|\bпогод\w* на (неделю|выходные|7 дней|семь дней)\b")
_QUESTION = re.compile(
    r"\b(когда|в какой день|какого числа|во сколько|сколько дней|недел\w*|выходн\w*|ближайш\w*|следующ\w*|"
    r"до конца|теплее|холоднее|потеплеет|похолодает|самы[йем] (тепл|холод|жарк|дождлив|солнечн)\w*|"
    r"шашлык\w*|прогулк\w*|велосипед\w*|помыть машину|мыть машину|закончится|кончится|прекратится|перестанет)\b")
_CITY = re.compile(r"\b(?:в|во)\s+(?P<city>[а-яё-]+(?:\s+[а-яё-]+){0,2})")
_DAY_WORDS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
_WEEKDAYS = {"понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3, "пятницу": 4, "пятница": 4,
             "субботу": 5, "суббота": 5, "воскресенье": 6}
_STOP_AFTER_CITY = {"сегодня", "завтра", "послезавтра", "сейчас", "утром", "днём", "днем", "вечером", "ночью", "улице",
                    "будет", "на", "какая", "какой", "градусов", "погода", "этой", "следующей", "выходные", "неделе",
                    "эту", "этот", "эти", "ближайшие", "ближайшее", "следующую", "следующий", "сколько", "течение",
                    *_WEEKDAYS}
_RAIN = set(range(51, 68)) | {80, 81, 82, 95, 96, 99}
_SNOW = set(range(71, 78)) | {85, 86}
_KIND_CODES = {"дождь": _RAIN, "снег": _SNOW, "гроза": {95, 96, 99}, "осадки": _RAIN | _SNOW}

# Part of the day -> hours of the hourly forecast ("будет ли дождь вечером").
_PARTS = {"утром": "утро", "утро": "утро", "днем": "день", "днём": "день", "вечером": "вечер", "вечер": "вечер",
          "ночью": "ночь", "ночь": "ночь"}
_HOURS = {"утро": range(6, 12), "день": range(12, 18), "вечер": range(18, 24), "ночь": range(0, 6)}
_PART_SAY = {"утро": "Утром", "день": "Днём", "вечер": "Вечером", "ночь": "Ночью"}

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
        if not (_TRIGGER.search(text) or _TRIGGER_ASK.search(text)):
            return None
        words = set(re.findall(r"[а-яё]+", text))
        day = next((d for w, d in _DAY_WORDS.items() if w in words), None)
        weekday = next((d for w, d in _WEEKDAYS.items() if w in words), None)
        if day is None and weekday is not None:
            day = (weekday - dt.date.today().weekday()) % 7
        day = day or 0
        part = next((p for w, p in _PARTS.items() if w in words), "")
        city = ""
        m = _CITY.search(text)
        if m:
            words = []
            for w in m.group("city").split():
                if w in _STOP_AFTER_CITY:
                    break
                words.append(w)
            city = " ".join(words)
        if _QUESTION.search(text):
            # "когда будет следующий дождь", "в какой день на неделе теплее всего": answered from the week's data.
            return Intent(self.name, "ask", {"city": city, "question": text}, text)
        return Intent(self.name, "forecast", {"city": city, "day": day, "part": part}, text)

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

    @staticmethod
    def _part_of_day(data: dict, day: int, part: str, place: str) -> str:
        """Hourly forecast for the morning / afternoon / evening / night of the asked day."""
        hourly = data["hourly"]
        target = (dt.date.today() + dt.timedelta(days=day + (1 if part == "ночь" and day == 0 and
                                                                dt.datetime.now().hour >= 6 else 0))).isoformat()
        rows = [i for i, t in enumerate(hourly["time"]) if t.startswith(target) and int(t[11:13]) in _HOURS[part]]
        if not rows:
            return "Почасового прогноза на это время нет."
        temps = [hourly["temperature_2m"][i] for i in rows]
        rain = max(hourly["precipitation_probability"][i] or 0 for i in rows)
        codes = [hourly["weather_code"][i] for i in rows]
        desc = WMO.get(max(set(codes), key=codes.count), "")
        when = _PART_SAY[part] + (" завтра" if day == 1 else " послезавтра" if day == 2 else "")
        text = f"{when} {in_place(place)} {_range(min(temps), max(temps))}, {desc}."
        if rain >= 30:
            text += f" Вероятность осадков до {rain}%."
        elif rain:
            text += " Осадков почти не будет."
        return text

    async def _fetch(self, city: str, days: int) -> tuple[dict, str] | Reply:
        got = await self._fetch_once(city, days)
        if isinstance(got, Reply) and got.fallthrough:  # a dropped connection: one more try
            got = await self._fetch_once(city, days)
        if isinstance(got, Reply):
            got.fallthrough = False
        return got

    async def _fetch_once(self, city: str, days: int) -> tuple[dict, str] | Reply:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                geo = await self._geocode(client, city)
                if not geo:
                    return Reply(f"Не нашёл город {city}.")
                lat, lon, place = geo
                r = await client.get("https://api.open-meteo.com/v1/forecast", params={
                    "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": days,
                    "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max,"
                             "precipitation_sum,wind_speed_10m_max",
                    "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code",
                    "wind_speed_unit": "ms",
                })
                r.raise_for_status()
                return r.json(), place
        except httpx.HTTPError as exc:
            log.warning("Open-Meteo: %s %s", type(exc).__name__, exc)
            return Reply("Сервис погоды сейчас не отвечает.", fallthrough=True)

    async def handle(self, intent: Intent) -> Reply:
        city = str(intent.slots.get("city") or "").strip() or self.app.cfg.assistant.default_city
        if intent.action == "ask":
            got = await self._fetch(city, 8)
            if isinstance(got, Reply):
                return got
            return self._answer(str(intent.slots.get("question") or intent.text), *got)
        day = max(0, min(int(intent.slots.get("day") or 0), 7))
        got = await self._fetch(city, day + 2)
        if isinstance(got, Reply):
            return got
        data, place = got

        part = str(intent.slots.get("part") or "")
        if part and data.get("hourly"):
            return Reply(self._part_of_day(data, day, part, place))
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
            date = dt.date.today() + dt.timedelta(days=day)
            when = "Завтра" if day == 1 else "Послезавтра" if day == 2 else _on_day(date).capitalize()
            text = f"{when} {in_place(place)} {_range(tmin, tmax)}, {desc}.{rain_txt}"
        return Reply(text)

    # ---------------------------------------------------------------- questions about the week
    def _answer(self, question: str, data: dict, place: str) -> Reply:
        kind = next((k for k, pat in (("гроза", "гроз"), ("снег", r"снег|снегопад"), ("дождь", r"дожд|ливн|ливен|морос"),
                                      ("осадки", "осадк")) if re.search(pat, question)), "")
        if kind and re.search(r"\b(когда|во сколько|в какой день|какого числа|следующ\w*|ближайш\w*)\b|"
                              r"(законч|кончит|прекрат|перестан)", question):
            return Reply(self._next_precip(data, kind, place, ending=bool(re.search(r"законч|кончит|прекрат|перестан",
                                                                                   question))))
        return self._ask_model(question, data, place)

    @staticmethod
    def _next_precip(data: dict, kind: str, place: str, ending: bool = False) -> str:
        """When the next rain / snow starts (or the current one stops), from the hourly forecast."""
        h = data["hourly"]
        codes = _KIND_CODES[kind]
        now = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
        times = [dt.datetime.fromisoformat(t) for t in h["time"]]
        prob = [p or 0 for p in h.get("precipitation_probability") or [0] * len(times)]
        amount = [a or 0 for a in h.get("precipitation") or [0] * len(times)]

        def wet(i: int) -> bool:  # a likely one: a 15 % chance of a drizzle is not "the next rain"
            return h["weather_code"][i] in codes and (prob[i] >= 40 or (amount[i] >= 0.5 and prob[i] >= 25))

        hours = [i for i, t in enumerate(times) if t >= now]
        if not hours:
            return "Почасового прогноза сейчас нет."
        first = hours[0]
        what = {"дождь": "Дождь", "снег": "Снег", "гроза": "Гроза", "осадки": "Осадки"}[kind]
        if wet(first):
            end = next((i for i in hours if not wet(i)), None)
            many = kind == "осадки"
            if end is None:
                return f"{what} {in_place(place)} не {'прекратятся' if many else 'прекратится'} до конца прогноза."
            return (f"{what} {in_place(place)} уже {'идут' if many else 'идёт'} и "
                    f"{'закончатся' if many else 'закончится'} {_around(times[end])}.")
        if ending:
            return f"Сейчас {in_place(place)} {kind_gen(kind)} нет."
        start = next((i for i in hours if wet(i)), None)
        if start is None:
            text = f"В ближайшую неделю {kind_gen(kind)} {in_place(place)} не ожидается."
            maybe = next((i for i in hours if h["weather_code"][i] in codes and amount[i] >= 0.5), None)
            if maybe is not None:
                text += f" Небольшой шанс {_around(times[maybe])}, вероятность {prob[maybe]}%."
            return text
        end = next((i for i in hours if i > start and not wet(i)), hours[-1])
        peak = max(prob[start:end + 1] or [0])
        mm = sum(amount[start:end + 1])
        many = kind == "осадки"
        text = f"{what} {in_place(place)} {'начнутся' if many else 'начнётся'} {_around(times[start])}"
        hours_long = max(1, end - start)
        text += f" и {'продлятся' if many else 'продлится'} около {hours_long} {plural(hours_long, 'часа', 'часов', 'часов')}" if hours_long > 1 else ""
        text += (f", вероятность {peak}%" if peak >= 50 else f", но вероятность всего {peak}%") if peak else ""
        if mm >= 1:
            text += f", до {round(mm)} мм"
        return text + "."

    def _ask_model(self, question: str, data: dict, place: str) -> Reply:
        """Unusual but sensible questions ("в какой день на неделе теплее", "можно ли в субботу на шашлыки"):
        the model answers from the real forecast instead of a template."""
        now = dt.datetime.now()
        cur = data.get("current") or {}
        d = data["daily"]
        rows = [f"Сейчас: {round(cur.get('temperature_2m', 0)):+d}°, {WMO.get(cur.get('weather_code'), '')}, ветер "
                f"{round(cur.get('wind_speed_10m', 0))} м/с"] if cur else []
        for i, day in enumerate(d["time"]):
            date = dt.date.fromisoformat(day)
            name = "сегодня" if i == 0 else "завтра" if i == 1 else _on_day(date)
            rows.append(f"{name} ({date:%d.%m}): {round(d['temperature_2m_min'][i]):+d}…{round(d['temperature_2m_max'][i]):+d}°, "
                        f"{WMO.get(d['weather_code'][i], '')}, осадки {d['precipitation_sum'][i] or 0:.1f} мм "
                        f"(вероятность {d['precipitation_probability_max'][i] or 0}%), ветер до "
                        f"{round(d['wind_speed_10m_max'][i] or 0)} м/с")
        system = (f"Ты отвечаешь на вопрос о погоде {in_place(place)} строго по прогнозу ниже. 1–2 коротких предложения "
                  f"по-русски, без markdown, списков и вступлений. Дни называй словами (сегодня, завтра, в субботу). "
                  f"Если в прогнозе нет ответа — так и скажи.\nСейчас {now:%d.%m %H:%M}.\nПрогноз:\n" + "\n".join(rows))
        chain = ["local", *[p for p in self.app.cfg.llm.info_chain if p != "local"]]
        return Reply(stream=self.app.llm.stream(chain, [Msg("system", system), Msg("user", question)], web=False,
                                                max_tokens=200))


_DAYS_ACC = ["в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье"]


def _on_day(date: dt.date) -> str:
    return _DAYS_ACC[date.weekday()]


def _around(at: dt.datetime) -> str:
    """'сегодня около 15:00', 'завтра около 9:00', 'в четверг около 18:00'."""
    delta = (at.date() - dt.date.today()).days
    day = "сегодня" if delta == 0 else "завтра" if delta == 1 else "послезавтра" if delta == 2 else _on_day(at.date())
    return f"{day} около {at.hour}:00"


def kind_gen(kind: str) -> str:
    return {"дождь": "дождя", "снег": "снега", "гроза": "грозы", "осадки": "осадков"}[kind]


def create() -> Skill:
    return WeatherSkill()
