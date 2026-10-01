"""Text cleanup before synthesis: markdown, numbers with agreeing units, money, dates, Latin words.

The voices read only Cyrillic words, so everything else is spelled out the way a Russian speaker says it:
"+24 °C, Ю-З 2 м/с" -> "плюс двадцать четыре градуса, ветер юго-западный два метра в секунду",
"84,43 ₽ за 1 USD" -> "восемьдесят четыре рубля сорок три копейки за один доллар",
"RTX 3060" -> "эр тэ икс тридцать шестьдесят", "1 октября 2024 г." -> "первое октября две тысячи двадцать четвёртого года".
"""
from __future__ import annotations

import re
from functools import lru_cache

from num2words.lang_RU import Num2Word_RU

from assistant.tts import english

_N = Num2Word_RU()

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_MD_MARKS = re.compile(r"[*_`#>|~]+")
_BULLET = re.compile(r"^\s*(?:[-•]|\d+[.)])\s+", re.M)
_CITE = re.compile(r"\[\d+(?:,\s*\d+)*\]|【[^】]*】")
_SPACES = re.compile(r"[ \t]+")

_TYPOGRAPHY = str.maketrans({" ": " ", " ": " ", " ": " ", " ": " ", "≈": " около ",
                              "~": " около ", "×": " на ", "→": ", ", "•": ", ", "−": "-", "№": " номер ",
                              "±": " плюс-минус ", "≥": " не меньше ", "≤": " не больше ", "=": " равно "})


def clean_for_speech(text: str) -> str:
    """Engine-independent cleanup."""
    text = text.translate(_TYPOGRAPHY)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _CITE.sub("", text)
    text = _BULLET.sub("", text)
    text = _MD_MARKS.sub("", text)
    text = text.replace("\n", ". ").replace("..", ".")
    return _SPACES.sub(" ", text).strip(" .") + ("" if text.rstrip().endswith(("?", "!", "…")) else ".")


# ---------------------------------------------------------------- numbers
_NUMBER = r"-?\d+(?:[.,]\d+)?"


def plural_form(value: float, forms: tuple[str, str, str]) -> str:
    """forms = (один градус, два градуса, пять градусов); fractions take the second form."""
    if value != int(value):
        return forms[1]
    n = abs(int(value))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _to_float(raw: str) -> float:
    return float(raw.replace(" ", "").replace(",", "."))


def say_number(raw: str | float, gender: str = "m", case: str = "n") -> str:
    """'84,43' -> 'восемьдесят четыре целых сорок три сотых', '1,5' -> 'полтора', 21 (f, acc) -> 'двадцать одну'."""
    value = _to_float(raw) if isinstance(raw, str) else float(raw)
    if value == int(value):
        try:
            return _N.to_cardinal(int(value), case=case, gender=gender, animate=False)
        except (ValueError, IndexError, KeyError):
            return _N.to_cardinal(int(value))
    if abs(value) == 1.5 and case in ("n", "a"):
        return ("минус " if value < 0 else "") + ("полторы" if gender == "f" else "полтора")
    text = raw if isinstance(raw, str) else str(raw)
    try:
        return _N.to_cardinal(float(text.replace(" ", "").replace(",", ".")))
    except (ValueError, OverflowError):
        return text


@lru_cache(maxsize=1)
def _morph():
    from assistant.nlu import _morph as get

    return get()


def _agree(word: str) -> tuple[str, str]:
    """(gender, case) for a number followed by `word`: "21 минуту" needs "двадцать одну"."""
    if not re.fullmatch(r"[а-яё]+", word.lower()):
        return "m", "n"
    try:
        tag = _morph().parse(word.lower())[0].tag
    except Exception:
        return "m", "n"
    gender = {"femn": "f", "neut": "n"}.get(str(tag.gender), "m")
    case = "a" if tag.case == "accs" else "n"
    return gender, case


# ---------------------------------------------------------------- units and money
def _unit(pattern: str, one: str, few: str, many: str, gender: str = "m"):
    return (re.compile(rf"(?:\b(?P<prep>{_GEN_PREPS})\s+)?(?<![\w,.])({_NUMBER})\s*(?:{pattern})(?![\w/])"),
            (one, few, many), gender)


# After these prepositions a number and its unit are in the genitive: "из 32 ГБ" -> "из тридцати двух гигабайт".
_GEN_PREPS = "(?i:из|от|до|около|более|менее|свыше|без|для|у|кроме|после)"


def _with_unit(m: re.Match[str], forms: tuple[str, str, str], gender: str) -> str:
    raw, prep = m.group(2), m.group("prep")
    value = _to_float(raw)
    if prep and value == int(value):
        n = abs(int(value))
        noun = forms[1] if n % 10 == 1 and n % 100 != 11 else forms[2]   # gen. singular / gen. plural
        return f"{prep} {say_number(raw, gender, 'g')} {noun}"
    return (f"{prep} " if prep else "") + f"{say_number(raw, gender)} {plural_form(value, forms)}"


_UNITS = [
    _unit(r"°\s*[CС]|°", "градус", "градуса", "градусов"),
    _unit(r"%", "процент", "процента", "процентов"),
    _unit(r"км/ч", "километр в час", "километра в час", "километров в час"),
    _unit(r"м/с", "метр в секунду", "метра в секунду", "метров в секунду"),
    _unit(r"мм рт\.?\s?ст\.?", "миллиметр ртутного столба", "миллиметра ртутного столба", "миллиметров ртутного столба"),
    _unit(r"м²|кв\.?\s?м\.?", "квадратный метр", "квадратных метра", "квадратных метров"),
    _unit(r"мм", "миллиметр", "миллиметра", "миллиметров"),
    _unit(r"см", "сантиметр", "сантиметра", "сантиметров"),
    _unit(r"км", "километр", "километра", "километров"),
    _unit(r"м", "метр", "метра", "метров"),
    _unit(r"кг", "килограмм", "килограмма", "килограммов"),
    _unit(r"г", "грамм", "грамма", "граммов"),
    _unit(r"мл", "миллилитр", "миллилитра", "миллилитров"),
    _unit(r"л", "литр", "литра", "литров"),
    _unit(r"[ТT][Бб]|TB", "терабайт", "терабайта", "терабайт"),
    _unit(r"[ГG][Бб]|GB", "гигабайт", "гигабайта", "гигабайт"),
    _unit(r"[МM][Бб]|MB", "мегабайт", "мегабайта", "мегабайт"),
    _unit(r"[КK][Бб]|KB", "килобайт", "килобайта", "килобайт"),
    _unit(r"Гбит/с|Gbps", "гигабит в секунду", "гигабита в секунду", "гигабит в секунду"),
    _unit(r"Мбит/с|Mbps", "мегабит в секунду", "мегабита в секунду", "мегабит в секунду"),
    _unit(r"ГГц|GHz", "гигагерц", "гигагерца", "гигагерц"),
    _unit(r"МГц|MHz", "мегагерц", "мегагерца", "мегагерц"),
    _unit(r"Гц|Hz", "герц", "герца", "герц"),
    _unit(r"кВт·?ч", "киловатт-час", "киловатт-часа", "киловатт-часов"),
    _unit(r"кВт|kW", "киловатт", "киловатта", "киловатт"),
    _unit(r"Вт", "ватт", "ватта", "ватт"),
    _unit(r"мАч|mAh", "миллиампер-час", "миллиампер-часа", "миллиампер-часов"),
    _unit(r"мс|ms", "миллисекунда", "миллисекунды", "миллисекунд", "f"),
    _unit(r"сек\.?", "секунда", "секунды", "секунд", "f"),
    _unit(r"мин\.?", "минута", "минуты", "минут", "f"),
    _unit(r"ч\.?", "час", "часа", "часов"),
    _unit(r"шт\.?", "штука", "штуки", "штук", "f"),
    _unit(r"чел\.?", "человек", "человека", "человек"),
    _unit(r"FPS|fps|к/с", "кадр в секунду", "кадра в секунду", "кадров в секунду"),
]

_SCALE = {"тыс": (("тысяча", "тысячи", "тысяч"), "f"), "млн": (("миллион", "миллиона", "миллионов"), "m"),
          "млрд": (("миллиард", "миллиарда", "миллиардов"), "m"), "трлн": (("триллион", "триллиона", "триллионов"), "m")}
_SCALE_WORDS = r"(?:тыс|млн|млрд|трлн)\.?|тысяч[аи]?|миллион(?:а|ов)?|миллиард(?:а|ов)?|триллион(?:а|ов)?"

# name: (forms, gender, minor-unit forms, regex of what follows the number, regex of a symbol before it)
_MONEY = {
    "rub": (("рубль", "рубля", "рублей"), "m", ("копейка", "копейки", "копеек"),
            r"₽|руб\.?|р\.(?=\s|$)|RUB|рубл(?:ь|я|ей)", r"₽"),
    "usd": (("доллар", "доллара", "долларов"), "m", ("цент", "цента", "центов"),
            r"\$|USD|долл\.?|доллар(?:а|ов)?", r"\$"),
    "eur": (("евро", "евро", "евро"), "n", ("цент", "цента", "центов"), r"€|EUR|евро", r"€"),
    "cny": (("юань", "юаня", "юаней"), "m", None, r"¥|CNY|юан(?:ь|я|ей)", r"¥"),
    "gbp": (("фунт", "фунта", "фунтов"), "m", ("пенс", "пенса", "пенсов"), r"£|GBP|фунт(?:а|ов)?", r"£"),
    "btc": (("биткоин", "биткоина", "биткоинов"), "m", None, r"BTC|биткоин(?:а|ов)?", r"₿"),
}
_MONEY_GEN = {"rub": "рубля", "usd": "доллара", "eur": "евро", "cny": "юаня", "gbp": "фунта", "btc": "биткоина",
              "jpy": "иены", "eth": "эфира"}
_PAIR = re.compile(r"\b(USD|EUR|CNY|GBP|BTC|RUB)\s*/\s*(RUB|USD|EUR)\b")
_PAIR_DAT = {"RUB": "рублю", "USD": "доллару", "EUR": "евро"}
_PAIR_NOM = {"USD": "доллар", "EUR": "евро", "CNY": "юань", "GBP": "фунт", "BTC": "биткоин", "RUB": "рубль"}


def _scale_key(word: str) -> str:
    w = word.rstrip(".")
    return ("тыс" if w.startswith("тыс") else "млрд" if w.startswith(("млрд", "миллиард"))
            else "трлн" if w.startswith(("трлн", "триллион")) else "млн")


def _amount(raw: str, scale: str | None, cur: tuple) -> str:
    forms, gender, minor, *_ = cur
    value = _to_float(raw)
    if scale:
        sforms, sgender = _SCALE[_scale_key(scale)]
        return f"{say_number(raw, sgender)} {plural_form(value, sforms)} {forms[2]}"
    whole, _, frac = raw.replace(" ", "").replace(".", ",").partition(",")
    if minor and frac and len(frac) == 2:
        w, f = int(whole), int(frac)
        text = f"{say_number(whole, gender)} {plural_form(w, forms)}"
        if f:
            text += f" {say_number(str(f), 'f' if minor[0].endswith('а') else 'm')} {plural_form(f, minor)}"
        return text
    return f"{say_number(raw, gender)} {plural_form(value, forms)}"


def _money(text: str) -> str:
    text = _PAIR.sub(lambda m: f"{_PAIR_NOM[m.group(1)]} к {_PAIR_DAT.get(m.group(2), m.group(2))}", text)
    for cur in _MONEY.values():
        after, before = cur[3], cur[4]
        # "$1,5 млрд", "₽100"
        text = re.sub(rf"(?:{before})\s?({_NUMBER}(?: \d{{3}})*)(?:\s?({_SCALE_WORDS}))?(?![\w])",
                      lambda m, c=cur: _amount(m.group(1), m.group(2), c), text)
        # "84,43 ₽", "1,2 млн долларов", "100 USD"
        text = re.sub(rf"(?<![\w,.])({_NUMBER}(?: \d{{3}})*)\s?(?:({_SCALE_WORDS})\s?)?(?:{after})(?![\w])",
                      lambda m, c=cur: _amount(m.group(1), m.group(2), c), text)
    return text


def _units(text: str) -> str:
    for pattern, forms, gender in _UNITS:
        text = pattern.sub(lambda m, f=forms, g=gender: _with_unit(m, f, g), text)
    # "15 млн жителей", "2 тыс." without money
    text = re.sub(rf"(?<![\w,.])({_NUMBER})\s?(тыс|млн|млрд|трлн)\.?(?![\w])",
                  lambda m: f"{say_number(m.group(1), _SCALE[m.group(2)][1])} "
                            f"{plural_form(_to_float(m.group(1)), _SCALE[m.group(2)][0])}", text)
    return text


# ---------------------------------------------------------------- ordinals: years, dates, centuries, "5-й"
_MONTHS = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
_YEAR_CASE = {"году": ("p", False), "года": ("g", False), "г.": ("g", False), "г": ("g", False), "год": ("n", False),
              "годом": ("i", False), "годах": ("p", True), "годов": ("g", True), "годы": ("n", True), "гг.": ("p", True)}
_SUFFIX = {"й": ("n", "m", False), "ый": ("n", "m", False), "ой": ("n", "m", False), "я": ("n", "f", False),
           "ая": ("n", "f", False), "е": ("n", "n", False), "ое": ("n", "n", False), "го": ("g", "m", False),
           "му": ("d", "m", False), "м": ("p", "m", False), "ю": ("a", "f", False), "ую": ("a", "f", False),
           "х": ("p", "m", True), "ми": ("i", "m", True)}
_DATE_PREP = {"до": "g", "с": "g", "со": "g", "от": "g", "после": "g", "около": "g", "к": "d", "по": "a", "на": "a"}
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def _ordinal(n: int, case: str = "n", gender: str = "m", plural: bool = False) -> str:
    try:
        return _N.to_ordinal(n, case=case, gender=gender, plural=plural, animate=False)
    except (ValueError, IndexError, KeyError):
        return _N.to_cardinal(n)


def _roman(s: str) -> int:
    total = 0
    for a, b in zip(s, s[1:] + " "):
        v = _ROMAN[a]
        total += -v if b in _ROMAN and _ROMAN[b] > v else v
    return total


def _ordinals(text: str) -> str:
    # "в 2024 году", "2024 г.", "в 1990-х годах"
    text = re.sub(r"\b(1\d{3}|20\d{2}|21\d{2})(?:-х)?\s+(году|года|годом|годах|годов|годы|год|гг\.|г\.|г)(?![\w])",
                  lambda m: f"{_ordinal(int(m.group(1)), _YEAR_CASE[m.group(2)][0], plural=_YEAR_CASE[m.group(2)][1])} "
                            f"{'годах' if m.group(2) == 'гг.' else 'года' if m.group(2) in ('г.', 'г') else m.group(2)}",
                  text)
    # "в 90-х", "10-й", "3-я", "5-го"
    text = re.sub(r"\b(\d+)-(ый|ой|ая|ое|ую|й|я|е|го|му|м|ю|х|ми)\b",
                  lambda m: _ordinal(int(m.group(1)), *_SUFFIX[m.group(2)][:2], plural=_SUFFIX[m.group(2)][2]), text)
    # "1 октября" -> "первое октября", "до 5 мая" -> "до пятого мая"

    def date(m: re.Match[str]) -> str:
        prep = (m.group(1) or "").strip().lower()
        return f"{m.group(1) or ''}{_ordinal(int(m.group(2)), _DATE_PREP.get(prep, 'n'), 'n')} {m.group(3)}"

    text = re.sub(rf"(\b(?:до|с|со|от|после|около|к|по|на)\s+)?\b([12]?\d|3[01])\s+({_MONTHS})\b", date, text)
    # "XX век", "в XXI веке"
    text = re.sub(r"\b([IVXLC]{1,7})\s+(век|века|веке|веком|веков)\b",
                  lambda m: f"{_ordinal(_roman(m.group(1)), {'век': 'n', 'века': 'g', 'веке': 'p', 'веком': 'i', 'веков': 'g'}[m.group(2)], plural=m.group(2) == 'веков')} {m.group(2)}",
                  text)
    return text


# ---------------------------------------------------------------- misc
_ABBR = [
    (re.compile(r"\bт\.\s?е\."), "то есть"), (re.compile(r"\bт\.\s?д\."), "так далее"),
    (re.compile(r"\bт\.\s?п\."), "тому подобное"), (re.compile(r"\bт\.\s?к\."), "так как"),
    (re.compile(r"\bт\.\s?н\."), "так называемый"), (re.compile(r"\bи др\."), "и другие"),
    (re.compile(r"\bг\.\s+(?=[А-ЯЁ])"), "город "), (re.compile(r"\bул\.\s?"), "улица "),
    (re.compile(r"\bим\.\s?"), "имени "), (re.compile(r"\bсм\.\s+(?=[а-яё])"), "смотрите "),
    (re.compile(r"\bпр\.\s?(?=[А-ЯЁ])"), "проспект "),
]
_WIND = {"С": "северный", "Ю": "южный", "З": "западный", "В": "восточный", "СЗ": "северо-западный",
         "СВ": "северо-восточный", "ЮЗ": "юго-западный", "ЮВ": "юго-восточный"}
_WIND_RE = re.compile(r"\b(ветер|ветра|ветром)(,?\s+)(С|Ю|З|В|С-?З|С-?В|Ю-?З|Ю-?В)(?![\w-])")
_WIND_ALONE = re.compile(r"(?<![\w-])(С-З|С-В|Ю-З|Ю-В)(?![\w-])")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_THOUSANDS = re.compile(r"\b(\d{1,3}(?: \d{3})+)\b")
_RANGE = re.compile(r"(\d)\s*[–—-]\s*(?=\d)")
_NUM_WORD = re.compile(rf"(?:\b(?P<prep>{_GEN_PREPS})\s+)?(?<![\w,.])({_NUMBER})(?![\w])(?:\s+([а-яёА-ЯЁ]+))?")
_SYMBOLS = [
    (re.compile(r"°"), " градусов"), (re.compile(r"%"), " процентов"),
    (re.compile(r"\$"), " долларов"), (re.compile(r"€"), " евро"), (re.compile(r"₽"), " рублей"),
    (re.compile(r"&"), " и "), (re.compile(r"\s[—–-]\s"), ", "), (re.compile(r"/"), " "),
]
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'’]*(?:-[A-Za-z0-9]+)*")
_DOMAIN = re.compile(r"\b([A-Za-z0-9-]+)\.(com|ru|org|net|io|ai|dev|рф|me|app)\b")
_TLD = {"com": "ком", "ru": "ру", "org": "орг", "net": "нет", "io": "ай о", "ai": "эй ай", "dev": "дев", "рф": "эр эф",
        "me": "ми", "app": "апп"}
_MODEL = re.compile(r"\b(RTX|GTX|GT|RX|Arc\s?[AB]|Radeon|GeForce|Quadro|i[3579]-?|Ryzen\s?[3579]\s|Core\s?i[3579]-?)\s?(\d{3,5})([A-Za-z]*)\b")
_VERSION = re.compile(r"\b(?:([A-Za-z][\w+#-]*|[Вв]ерси[яиюей])\s+)v?(\d+)\.(\d+)(?:\.(\d+))?\b|\bv?(\d+)\.(\d+)\.(\d+)\b")
_CODE_ALONE = re.compile(r"\b(USD|EUR|RUB|CNY|GBP|JPY|BTC|ETH)\b")


def _version(m: re.Match[str]) -> str:
    """"Python 3.12" -> "Python 3 точка 12", "v20.11.1" -> "20 точка 11 точка 1"."""
    head = m.group(1)
    parts = [p for p in m.groups()[1:] if p]
    return (f"{head} " if head else "") + " точка ".join(parts)


def _numbers(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        prep, raw, nxt = m.group("prep"), m.group(2), m.group(3)
        gender, case = _agree(nxt) if nxt else ("m", "n")
        if prep and "," not in raw and "." not in raw:
            case = "g"  # "до 21 минуты" -> "до двадцати одной минуты"
        return (f"{prep} " if prep else "") + say_number(raw, gender, case) + (f" {nxt}" if nxt else "")

    return _NUM_WORD.sub(repl, text)


def _latin(text: str) -> str:
    text = _DOMAIN.sub(lambda m: f"{m.group(1)} точка {_TLD[m.group(2).lower()]}", text)
    text = _CODE_ALONE.sub(lambda m: _MONEY_GEN[m.group(1).lower()], text)
    return _LATIN_TOKEN.sub(lambda m: english.word(m.group(0)), text)


def normalize_ru(text: str) -> str:
    """Full normalization for engines that can only read Cyrillic words."""
    text = clean_for_speech(english.special(text))  # before markdown cleanup eats "#" of "C#"
    for pattern, repl in _ABBR:
        text = pattern.sub(repl, text)
    text = _WIND_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_WIND[m.group(3).replace('-', '')]}", text)
    text = _WIND_ALONE.sub(lambda m: _WIND[m.group(1).replace("-", "")], text)
    text = _TIME.sub(lambda m: f"{int(m.group(1))} {m.group(2)}" if m.group(2) != "00" else f"{int(m.group(1))} ноль ноль",
                     text)
    text = re.sub(r"\+(?=\s*\d)", "плюс ", text).replace("+", " плюс ")  # "+" is a stress mark for the voices
    text = _MODEL.sub(lambda m: f"{m.group(1).rstrip('-')} {english.model_number(m.group(2))} {m.group(3)}", text)
    text = _VERSION.sub(_version, text)
    text = _THOUSANDS.sub(lambda m: m.group(1).replace(" ", ""), text)
    text = re.sub(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", " ", text)   # "4K", "PS5", "RTX3060"
    text = _RANGE.sub(r"\1 ", text)                                          # "5–7 минут"
    text = _ordinals(text)
    text = _money(text)
    text = _units(text)
    text = _numbers(text)
    for pattern, repl in _SYMBOLS:
        text = pattern.sub(repl, text)
    text = _latin(text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    return _SPACES.sub(" ", text).strip()
