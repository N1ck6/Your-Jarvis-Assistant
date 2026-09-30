"""Text cleanup before synthesis: markdown, numbers, symbols, Latin words."""
from __future__ import annotations

import re

from num2words import num2words

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_MD_MARKS = re.compile(r"[*_`#>|~]+")
_BULLET = re.compile(r"^\s*(?:[-•]|\d+[.)])\s+", re.M)
_CITE = re.compile(r"\[\d+(?:,\s*\d+)*\]|【[^】]*】")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_NUM = re.compile(r"(?<![\w])-?\d+(?:[.,]\d+)?")
_SPACES = re.compile(r"[ \t]+")

_SYMBOLS = [
    (re.compile(r"°\s*[CС]"), " градусов"),
    (re.compile(r"°"), " градусов"),
    (re.compile(r"%"), " процентов"),
    (re.compile(r"\+(?=\s*\d)"), "плюс "),
    (re.compile(r"(?<=\d)\s*км/ч"), " километров в час"),
    (re.compile(r"(?<=\d)\s*м/с"), " метров в секунду"),
    (re.compile(r"(?<=\d)\s*мм\b"), " миллиметров"),
    (re.compile(r"(?<=\d)\s*₽|\bруб\.(?=\s|$)"), " рублей"),
    (re.compile(r"\$"), " долларов"),
    (re.compile(r"€"), " евро"),
    (re.compile(r"&"), " и "),
    (re.compile(r"\s[—–-]\s"), ", "),
]

_LAT = {
    "sch": "щ", "sh": "ш", "ch": "ч", "zh": "ж", "kh": "х", "ts": "ц", "ya": "я", "yu": "ю", "yo": "ё",
    "ee": "и", "oo": "у", "th": "т", "ph": "ф", "ck": "к", "qu": "кв",
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "j": "дж",
    "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т",
    "u": "у", "v": "в", "w": "в", "x": "кс", "y": "й", "z": "з",
}
_KNOWN_LATIN = {
    "ai": "эй ай", "api": "апи", "gpt": "джи пи ти", "python": "пайтон", "windows": "виндоус",
    "google": "гугл", "youtube": "ютуб", "wi-fi": "вайфай", "wifi": "вайфай", "usb": "ю эс би",
    "ok": "окей", "iphone": "айфон", "android": "андроид", "linux": "линукс", "java": "джава",
    "javascript": "джаваскрипт", "html": "эйч ти эм эль", "css": "си эс эс", "sql": "эс кью эль",
    "cpu": "процессор", "gpu": "видеокарта", "ram": "оперативная память", "it": "ай ти",
    "steam": "стим", "telegram": "телеграм", "discord": "дискорд", "github": "гитхаб",
}
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z\-']*")


def _translit(word: str) -> str:
    low = word.lower()
    if low in _KNOWN_LATIN:
        return _KNOWN_LATIN[low]
    if word.isupper() and len(word) <= 4:  # abbreviations: letter by letter
        return " ".join(_LAT.get(ch, ch) for ch in low)
    out, i = [], 0
    while i < len(low):
        for size in (3, 2, 1):
            chunk = low[i:i + size]
            if chunk in _LAT:
                out.append(_LAT[chunk])
                i += size
                break
        else:
            i += 1
    return "".join(out)


def _number(match: re.Match[str]) -> str:
    raw = match.group(0).replace(",", ".")
    try:
        value = float(raw) if "." in raw else int(raw)
        return num2words(value, lang="ru")
    except (ValueError, OverflowError, NotImplementedError):
        return raw


def clean_for_speech(text: str) -> str:
    """Engine-independent cleanup (also used for Edge)."""
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _CITE.sub("", text)
    text = _BULLET.sub("", text)
    text = _MD_MARKS.sub("", text)
    text = text.replace("\n", ". ").replace("..", ".")
    return _SPACES.sub(" ", text).strip(" .") + ("" if text.rstrip().endswith(("?", "!", "…")) else ".")


def normalize_ru(text: str) -> str:
    """Full normalization for local engines that can only read Cyrillic words."""
    text = clean_for_speech(text)
    for pattern, repl in _SYMBOLS:
        text = pattern.sub(repl, text)
    text = _TIME.sub(lambda m: f"{int(m.group(1))} {m.group(2)}" if m.group(2) != "00" else f"{int(m.group(1))} ноль ноль", text)
    text = _NUM.sub(_number, text)
    text = _LATIN_WORD.sub(lambda m: _translit(m.group(0)), text)
    return _SPACES.sub(" ", text).strip()
