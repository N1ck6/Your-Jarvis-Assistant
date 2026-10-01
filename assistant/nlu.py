"""Small Russian language helpers: numbers in words, durations, wake word, city names."""
from __future__ import annotations

import re
from functools import lru_cache

from rapidfuzz import fuzz

_UNITS = {
    "ноль": 0, "один": 1, "одну": 1, "одна": 1, "одной": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
    "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
         "семьдесят": 70, "восемьдесят": 80, "девяносто": 90}
_HUNDREDS = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500}
_NUM_WORDS = {**_UNITS, **_TENS, **_HUNDREDS}


def words_to_numbers(text: str) -> str:
    """'через двадцать пять минут' -> 'через 25 минут'. Digits are kept as is."""
    tokens = text.split(" ")
    out: list[str] = []
    acc: int | None = None
    for tok in tokens:
        low = tok.lower().strip(",.!?")
        if low in _NUM_WORDS:
            val = _NUM_WORDS[low]
            if acc is None:
                acc = val
            elif (acc % 100 == 0 and val < 100) or (acc % 10 == 0 and acc % 100 != 0 and val < 10):
                acc += val
            else:
                out.append(str(acc))
                acc = val
            continue
        if acc is not None:
            out.append(str(acc))
            acc = None
        out.append(tok)
    if acc is not None:
        out.append(str(acc))
    return " ".join(out)


_DURATION_PARTS = re.compile(
    r"(?:(?P<num>\d+(?:[.,]\d+)?)\s*)?(?P<unit>час(?:а|ов)?|минут(?:у|ы)?|мин|секунд(?:у|ы)?|сек)\b"
)
_SPECIAL = [
    (re.compile(r"\bполчаса\b"), 30 * 60),
    (re.compile(r"\bполтора\s+часа\b"), 90 * 60),
    (re.compile(r"\bполторы\s+минуты\b"), 90),
    (re.compile(r"\bчетверть\s+часа\b"), 15 * 60),
    (re.compile(r"\bпар[уы]\s+минут\b"), 2 * 60),
    (re.compile(r"\bпар[уы]\s+часов\b"), 2 * 3600),
    (re.compile(r"\bпол\s*минуты\b"), 30),
]


def parse_duration(text: str) -> tuple[int, str] | None:
    """Returns (seconds, matched_text) for phrases like 'через 1 час 20 минут', 'на полчаса'."""
    low = words_to_numbers(text.lower())
    for pattern, seconds in _SPECIAL:
        m = pattern.search(low)
        if m:
            return seconds, m.group(0)
    total = 0
    spans: list[str] = []
    for m in _DURATION_PARTS.finditer(low):
        num = float(m.group("num").replace(",", ".")) if m.group("num") else 1.0
        unit = m.group("unit")
        mult = 3600 if unit.startswith("час") else 60 if unit.startswith("мин") else 1
        total += int(num * mult)
        spans.append(m.group(0))
    if total <= 0:
        return None
    return total, " ".join(spans)


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def say_duration(seconds: int) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    parts = []
    if h:
        parts.append(f"{h} {plural(h, 'час', 'часа', 'часов')}")
    if m:
        parts.append(f"{m} {plural(m, 'минуту', 'минуты', 'минут')}")
    if s and not h:
        parts.append(f"{s} {plural(s, 'секунду', 'секунды', 'секунд')}")
    return " ".join(parts) or "0 секунд"


async def sleep_until(unix_time: float, step: float = 15.0) -> None:
    """Waits by the wall clock: asyncio.sleep is monotonic and on Windows stops while the PC sleeps,
    so a plain sleep(3600) would fire late by however long the laptop was closed."""
    import asyncio
    import time

    while (left := unix_time - time.time()) > 0:
        await asyncio.sleep(min(left, step))


# ---------------------------------------------------------------- commands
_PUNCT = re.compile(r"[«»\"“”„!?;:()\[\]{}…,]")
_LONE_DOT = re.compile(r"(?<![\w])\.|\.(?![\w])")  # keeps dots inside "habr.com" and "3.5"
_DASH = re.compile(r"\s[—–-]+\s|^[—–-]+\s*")


_POLITE_HEAD = re.compile(
    r"^(?:(?:пожалуйста|будь (?:добр|добра|другом|любезен)|слушай|послушай|эй|ну|а|и|так|итак|давай|давайка|"
    r"можешь|можно|ты можешь|сможешь|не мог бы ты|не могли бы вы|мог бы ты|"
    r"(?:мне )?(?:нужно|надо) чтобы ты|я хочу чтобы ты|хочу чтобы ты|сделай так чтобы|быстро|срочно|скорее)\s+)+"
)
_POLITE_TAIL = re.compile(r"(?:\s+(?:пожалуйста|плиз|please|будь добр|срочно|сейчас же))+$")


# GigaAM writes some Russian words that sound English in Latin ("Стоп!" -> "Stop.").
_LATIN_WORDS = {"stop": "стоп", "yes": "да", "no": "нет", "okay": "окей", "ok": "ок", "pause": "пауза",
                "play": "плей", "next": "некст", "mute": "мьют", "jarvis": "джарвис"}
_LATIN_RE = re.compile(r"\b(" + "|".join(_LATIN_WORDS) + r")\b")


def normalize_command(text: str, strip_polite: bool = True) -> str:
    """'Джарвис, будь добр, открой «Телеграм»!' -> 'открой телеграм' (after strip_wake). Skills match on this."""
    t = text.lower().replace("ё", "е")
    t = _PUNCT.sub(" ", t)
    t = _LONE_DOT.sub(" ", t)
    t = _DASH.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = _LATIN_RE.sub(lambda m: _LATIN_WORDS[m.group(1)], t)
    if strip_polite:
        stripped = _POLITE_TAIL.sub("", _POLITE_HEAD.sub("", t)).strip()
        stripped = re.sub(r"\s+(?:пожалуйста|плиз)\b", "", stripped)
        # "расскажи мне шутку" -> "расскажи шутку": "мне" right after the verb carries no meaning for commands.
        stripped = re.sub(r"^(\S+) мне (?=\S)", r"\1 ", stripped)
        t = stripped or t  # "давай" alone stays "давай" (it is an answer)
    return t


_CMD_VERBS = (r"(?:открой|закрой|включи|выключи|поставь|найди|сверни|разверни|запусти|сделай|добавь|напомни|переключи|"
              r"переключись|убери|отмени|покажи|запиши|громче|тише|пауза|останови|продолжи|засеки|скажи|вычеркни|"
              r"перейди|верни|запомни|очисти|прочитай|переведи)")
_COMPOUND = re.compile(rf"\s+(?:и|а|а потом|потом|затем|и потом|и затем|после этого|а затем|и еще|а еще|плюс)\s+(?={_CMD_VERBS}\b)")


def split_compound(norm: str) -> list[str]:
    """'закрой браузер и включи музыку' -> ['закрой браузер', 'включи музыку'] (only before a command verb)."""
    return [p.strip() for p in _COMPOUND.split(norm) if p.strip()]


YES = re.compile(r"^(да|ага|угу|давай|подтверждаю|конечно|точно|верно|выполняй|делай|так точно|именно|ок|окей|yes)\b")
NO = re.compile(r"^(нет|не надо|не нужно|не стоит|отмена|отмени|отбой|не)\b")


# ---------------------------------------------------------------- wake word
_WORD = re.compile(r"[\wё-]+", re.I)
_LEADING_FILLER = re.compile(r"^(?:\s*(?:эй|слушай|скажи|пожалуйста|ну|а|так|окей|ок)[,!.\s]+)+", re.I)


def find_wake(text: str, phrases: list[str], min_ratio: int = 70) -> tuple[int, int] | None:
    """Span of the wake word in the text (fuzzy: 'Джарвис', 'Жарвис', 'Джервис')."""
    for m in _WORD.finditer(text):
        word = m.group(0).lower()
        word = _LATIN_WORDS.get(word, word)  # "Jarvis" from the recognizer
        if any(fuzz.ratio(word, p) >= min_ratio for p in phrases):
            return m.span()
    return None


def strip_wake(text: str, phrases: list[str], min_ratio: int = 70) -> str:
    span = find_wake(text, phrases, min_ratio)
    if span:
        text = (text[:span[0]] + " " + text[span[1]:])
    text = re.sub(r"^[\s,.!?;:—-]+", "", text)
    text = _LEADING_FILLER.sub("", text)
    text = re.sub(r"[\s,]+([?!.])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


# ---------------------------------------------------------------- morphology
@lru_cache(maxsize=1)
def _morph():
    import pymorphy3

    return pymorphy3.MorphAnalyzer(lang="ru")


def to_nominative(phrase: str) -> str:
    """'Нижнем Новгороде' -> 'Нижний Новгород', 'Казани' -> 'Казань'."""
    morph = _morph()
    out = []
    for w in phrase.split():
        parses = morph.parse(w)
        best = next((p for p in parses if "Geox" in p.tag), None) or parses[0]
        form = best.inflect({"nomn"})
        word = form.word if form else w.lower()
        parts = [p if p in ("на", "де", "ла") else p[:1].upper() + p[1:] for p in word.split("-")]
        out.append("-".join(parts))
    return " ".join(out)


def in_place(name: str) -> str:
    """'Москва' -> 'в Москве', 'Нижний Новгород' -> 'в Нижнем Новгороде'. Falls back to 'в городе X'."""
    morph = _morph()
    out = []
    for w in name.split():
        if "-" in w:
            return f"в городе {name}"
        parses = morph.parse(w)
        best = next((p for p in parses if "Geox" in p.tag or "ADJF" in p.tag), None)
        form = best.inflect({"loct"}) if best else None
        if not form:
            return f"в городе {name}"
        out.append(form.word[:1].upper() + form.word[1:])
    prep = "во" if out and out[0].lower().startswith(("вл", "вр", "фр")) else "в"
    return f"{prep} {' '.join(out)}"
