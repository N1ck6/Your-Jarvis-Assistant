"""Latin words for a Russian-only voice: how a Russian speaker would say "RTX 3060", "REST API", "Docker", "I can be more".

Order: curated dictionary (Russian IT/brand conventions: "докер", "пайтон", "эр тэ икс") -> letter-by-letter
for abbreviations -> CMU pronouncing dictionary (English phonemes -> Cyrillic) -> spelling rules.
The CMU dictionary (BSD, 3.6 MB) is downloaded once into models/cmudict and loaded on first use.
"""
from __future__ import annotations

import logging
import re
import threading

from assistant.paths import MODELS_DIR

log = logging.getLogger("tts")

CMU_URL = "https://raw.githubusercontent.com/cmusphinx/cmudict/master/cmudict.dict"
CMU_FILE = MODELS_DIR / "cmudict" / "cmudict.dict"

# English letter names as Russians say them in abbreviations ("ю эс би", "джи пи ю").
LETTERS = {
    "a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф", "g": "джи", "h": "эйч", "i": "ай",
    "j": "джей", "k": "кей", "l": "эл", "m": "эм", "n": "эн", "o": "о", "p": "пи", "q": "кью", "r": "ар",
    "s": "эс", "t": "ти", "u": "ю", "v": "ви", "w": "дабл ю", "x": "икс", "y": "уай", "z": "зет",
}

# Established Russian pronunciations. Keys are lowercase; values may hold several words.
WORDS: dict[str, str] = {
    # hardware
    "rtx": "эр тэ икс", "gtx": "джи ти икс", "rx": "эр икс", "nvidia": "энвидиа", "geforce": "джифорс",
    "radeon": "радеон", "amd": "эй эм ди", "ryzen": "райзен", "intel": "интел", "core": "кор", "xeon": "зеон",
    "threadripper": "тредриппер", "ti": "ти ай", "super": "супер", "xt": "экс ти", "xtx": "экс ти экс",
    "ram": "оперативка", "ssd": "эс эс ди", "hdd": "эйч ди ди", "nvme": "эн ви эм и", "usb": "ю эс би",
    "hdmi": "эйч ди эм ай", "displayport": "дисплей порт", "wi-fi": "вайфай", "wifi": "вайфай", "bluetooth": "блютус",
    "ddr4": "ди ди ар четыре", "ddr5": "ди ди ар пять", "cpu": "си пи ю", "gpu": "джи пи ю", "vram": "видеопамять",
    "laptop": "лэптоп", "macbook": "макбук", "iphone": "айфон", "ipad": "айпад", "airpods": "эйрподс",
    "playstation": "плейстейшн", "xbox": "иксбокс", "nintendo": "нинтендо", "switch": "свич", "steam": "стим",
    "deck": "дэк", "oled": "олед", "led": "лед", "ips": "ай пи эс", "fps": "эф пи эс", "hz": "герц",
    # companies and services
    "apple": "эпл", "google": "гугл", "microsoft": "майкрософт", "amazon": "амазон", "samsung": "самсунг",
    "xiaomi": "сяоми", "huawei": "хуавей", "honor": "хонор", "sony": "сони", "asus": "асус", "acer": "асер",
    "lenovo": "леново", "msi": "эм эс ай", "dell": "делл", "hp": "эйч пи", "logitech": "логитек", "razer": "рейзер",
    "tesla": "тесла", "spacex": "спейс икс", "starlink": "старлинк", "netflix": "нетфликс", "spotify": "спотифай",
    "youtube": "ютуб", "twitch": "твич", "tiktok": "тикток", "instagram": "инстаграм", "facebook": "фейсбук",
    "twitter": "твиттер", "reddit": "реддит", "telegram": "телеграм", "discord": "дискорд", "whatsapp": "вотсап",
    "viber": "вайбер", "skype": "скайп", "zoom": "зум", "teams": "тимс", "slack": "слак", "notion": "ноушн",
    "obsidian": "обсидиан", "figma": "фигма", "github": "гитхаб", "gitlab": "гитлаб", "gmail": "джимейл",
    "yandex": "яндекс", "ozon": "озон", "wildberries": "вайлдберриз", "aliexpress": "алиэкспресс", "ebay": "ибэй",
    "paypal": "пэйпал", "visa": "виза", "mastercard": "мастеркард", "openai": "оупен эй ай", "chatgpt": "чат джи пи ти",
    "gpt": "джи пи ти", "anthropic": "антропик", "claude": "клод", "gemini": "джемини", "llama": "лама",
    "mistral": "мистраль", "qwen": "квен", "deepseek": "дипсик", "ollama": "оллама", "groq": "грок", "grok": "грок",
    "copilot": "копайлот", "midjourney": "миджорни", "stable": "стейбл", "diffusion": "диффьюжн", "jarvis": "джарвис",
    "chrome": "хром", "firefox": "файрфокс", "edge": "эдж", "opera": "опера", "safari": "сафари", "brave": "брейв",
    "office": "офис", "word": "ворд", "excel": "эксель", "powerpoint": "пауэрпойнт", "outlook": "аутлук",
    "onedrive": "уан драйв", "dropbox": "дропбокс", "photoshop": "фотошоп", "adobe": "адоби", "premiere": "премьер",
    "blender": "блендер", "unity": "юнити", "unreal": "анриал", "obs": "о би эс",
    # software and IT
    "windows": "виндоус", "linux": "линукс", "ubuntu": "убунту", "debian": "дебиан", "fedora": "федора",
    "macos": "мак о эс", "ios": "ай о эс", "android": "андроид", "python": "пайтон", "java": "джава",
    "javascript": "джаваскрипт", "typescript": "тайпскрипт", "kotlin": "котлин", "swift": "свифт", "rust": "раст",
    "golang": "гоу", "php": "пи эйч пи", "ruby": "руби", "perl": "перл", "haskell": "хаскель", "scala": "скала",
    "react": "реакт", "vue": "вью", "angular": "ангуляр", "svelte": "свелт", "node": "ноуд", "nodejs": "ноуд джей эс",
    "django": "джанго", "flask": "фласк", "fastapi": "фаст эй пи ай", "spring": "спринг", "docker": "докер",
    "kubernetes": "кубернетес", "k8s": "кубернетес", "git": "гит", "nginx": "энджин икс", "apache": "апачи",
    "mysql": "май эс кью эль", "postgres": "постгрес", "postgresql": "постгрес", "sqlite": "эс кью лайт",
    "mongodb": "монго ди би", "redis": "редис", "kafka": "кафка", "rabbitmq": "рэббит эм кью", "elasticsearch": "эластиксёрч",
    "aws": "эй даблю эс", "azure": "ажур", "cloud": "клауд", "server": "сервер", "backend": "бэкенд",
    "frontend": "фронтенд", "fullstack": "фулстек", "devops": "девопс", "api": "эй пи ай", "rest": "рест",
    "restful": "рестфул", "graphql": "граф кью эль", "grpc": "джи ар пи си", "json": "джейсон", "xml": "экс эм эль",
    "yaml": "ямл", "html": "эйч ти эм эль", "css": "си эс эс", "http": "эйч ти ти пи", "https": "эйч ти ти пи эс",
    "url": "ю ар эл", "dns": "ди эн эс", "ip": "ай пи", "tcp": "ти си пи", "udp": "ю ди пи", "ssh": "эс эс эйч",
    "ftp": "эф ти пи", "vpn": "ви пи эн", "sql": "эс кью эль", "nosql": "ноу эс кью эль", "orm": "о эр эм",
    "sdk": "эс ди кей", "ide": "ай ди и", "ui": "ю ай", "ux": "ю экс", "ai": "эй ай", "ml": "эм эл",
    "llm": "эл эл эм", "nlp": "эн эл пи", "gui": "гуи", "cli": "си эл ай", "os": "о эс", "pc": "пи си",
    "tv": "ти ви", "it": "ай ти", "ok": "окей", "okay": "окей", "email": "имейл", "e-mail": "имейл",
    "online": "онлайн", "offline": "офлайн", "smart": "смарт", "gaming": "гейминг", "game": "гейм",
    "stream": "стрим", "streamer": "стример", "update": "апдейт", "download": "даунлоад", "upload": "аплоад",
    "login": "логин", "password": "пассворд", "browser": "браузер", "bug": "баг", "feature": "фича",
    "release": "релиз", "beta": "бета", "alpha": "альфа", "pro": "про", "max": "макс", "plus": "плюс",
    "ultra": "ультра", "mini": "мини", "lite": "лайт", "air": "эйр", "home": "хоум", "studio": "студио",
    "store": "стор", "play": "плей", "market": "маркет", "music": "мьюзик", "video": "видео", "news": "ньюс",
    "live": "лайв", "chat": "чат", "bot": "бот", "app": "апп", "apps": "аппс", "web": "веб", "dev": "дев",
    "framework": "фреймворк", "deploy": "деплой", "commit": "коммит", "push": "пуш", "pull": "пулл",
    "request": "реквест", "merge": "мёрж", "branch": "бранч", "token": "токен", "tokens": "токены",
    "prompt": "промпт", "pipeline": "пайплайн", "dataset": "датасет", "benchmark": "бенчмарк", "cache": "кэш",
    "proxy": "прокси", "firewall": "файрвол", "router": "роутер", "hosting": "хостинг", "domain": "домен",
    "startup": "стартап", "manager": "менеджер", "team": "тим", "lead": "лид", "senior": "сеньор",
    "junior": "джуниор", "middle": "мидл", "hr": "эйч ар", "ceo": "си и о", "cto": "си ти о", "pdf": "пи ди эф",
    "jpeg": "джейпег", "jpg": "джейпег", "png": "пи эн джи", "gif": "гиф", "mp3": "эм пи три", "mp4": "эм пи четыре",
    "wav": "вав", "zip": "зип", "exe": "экзешник", "nft": "эн эф ти", "crypto": "крипто",
    # money
    "usd": "доллар", "eur": "евро", "rub": "рубль", "cny": "юань", "gbp": "фунт", "jpy": "иена",
    "btc": "биткоин", "bitcoin": "биткоин", "eth": "эфир", "ethereum": "эфириум", "usdt": "ю эс ди ти",
}
# "USD 84" / "84 USD": money codes get proper Russian forms next to numbers.
CURRENCY_CODES = {"usd", "eur", "rub", "cny", "gbp", "jpy", "btc", "eth"}

_SPECIAL = [
    (re.compile(r"(?<![\w+])C\+\+"), "си плюс плюс"),
    (re.compile(r"(?<![\w#])C#"), "си шарп"),
    (re.compile(r"(?<![\w#])F#"), "эф шарп"),
    (re.compile(r"(?<![\w.])\.NET\b", re.I), "дот нет"),
    (re.compile(r"\b(\w+)\.js\b", re.I), r"\1 джей эс"),
    (re.compile(r"\bWi-?Fi\b", re.I), "вайфай"),
    (re.compile(r"\bE-mail\b", re.I), "имейл"),
]

_VOWEL_PH = {"AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY", "OW", "OY", "UH", "UW"}
_PH = {
    "AA": "а", "AE": "э", "AH": "а", "AO": "о", "AW": "ау", "AY": "ай", "EH": "е", "ER": "ер", "EY": "ей",
    "IH": "и", "IY": "и", "OW": "оу", "OY": "ой", "UH": "у", "UW": "у",
    "B": "б", "CH": "ч", "D": "д", "DH": "з", "F": "ф", "G": "г", "HH": "х", "JH": "дж", "K": "к", "L": "л",
    "M": "м", "N": "н", "NG": "нг", "P": "п", "R": "р", "S": "с", "SH": "ш", "T": "т", "TH": "с", "V": "в",
    "W": "у", "Y": "й", "Z": "з", "ZH": "ж",
}
# Y + vowel -> iotated Russian vowel.
_IOTATED = {"UW": "ю", "UH": "ю", "AA": "я", "AH": "я", "AE": "я", "EH": "е", "EY": "ей", "AO": "йо", "OW": "йоу",
            "IH": "и", "IY": "и", "ER": "ёр"}

_cmu: dict[str, str] | None = None
_cmu_lock = threading.Lock()
_cmu_failed = False


def _load_cmu() -> dict[str, str]:
    """word -> "K AE1 N" (strings, not lists: ~15 MB instead of ~45 MB). Loaded on the first unknown Latin word."""
    global _cmu, _cmu_failed
    if _cmu is not None:
        return _cmu
    with _cmu_lock:
        if _cmu is not None:
            return _cmu
        data: dict[str, str] = {}
        if not CMU_FILE.exists() and not _cmu_failed:
            try:
                from assistant.downloads import fetch

                fetch(CMU_URL, CMU_FILE, attempts=2, what="словарь английского произношения (3,6 МБ)")
            except Exception as exc:  # offline: spelling rules still work
                _cmu_failed = True
                log.warning("Словарь CMU недоступен: %s", exc)
        if CMU_FILE.exists():
            for line in CMU_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                word, _, phones = line.partition(" ")
                if "(" in word or not word.isalpha():
                    continue  # alternative pronunciations and "a.m."-like entries
                data[word] = phones.split("#")[0].strip()
        _cmu = data
        return _cmu


def preload() -> None:
    """Loads the dictionary in the background so the first English word does not wait for it."""
    threading.Thread(target=_load_cmu, name="cmudict", daemon=True).start()


def from_phonemes(phones: list[str]) -> str:
    """ARPAbet -> Cyrillic with a stress mark ('+' before the stressed vowel, the syntax of Silero and its stress model)."""
    out: list[str] = []
    vowels = sum(1 for p in phones if p.rstrip("012") in _VOWEL_PH)
    i = 0
    while i < len(phones):
        ph = phones[i]
        base, stress = ph.rstrip("012"), ph[-1] if ph[-1].isdigit() else ""
        prev = phones[i - 1].rstrip("012") if i else ""
        mark = "+" if stress == "1" and vowels > 1 else ""
        if base == "Y" and i + 1 < len(phones) and phones[i + 1].rstrip("012") in _IOTATED:
            nxt = phones[i + 1]
            soft = "ь" if prev and prev not in _VOWEL_PH and prev not in ("Y", "W") else ""
            mark = "+" if nxt[-1] == "1" and vowels > 1 else ""
            out.append(soft + mark + _IOTATED[nxt.rstrip("012")])
            i += 2
            continue
        letter = _PH.get(base, "")
        if base in ("EH", "EY", "ER") and (not prev or prev in _VOWEL_PH or prev == "W"):
            letter = {"EH": "э", "EY": "эй", "ER": "эр"}[base]
        out.append(mark + letter if base in _VOWEL_PH else letter)
        i += 1
    return "".join(out)


# Spelling rules for words missing from the dictionary ("Ollama", "Qwen"): rough but readable.
_RULES = [
    ("tion", "шн"), ("sion", "жн"), ("ture", "чер"), ("ight", "айт"), ("igh", "ай"), ("ough", "ау"),
    ("sch", "ш"), ("tch", "ч"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ph", "ф"), ("th", "т"),
    ("wh", "у"), ("ck", "к"), ("qu", "кв"), ("ee", "и"), ("ea", "и"), ("oo", "у"), ("ou", "ау"), ("ow", "оу"),
    ("ai", "ей"), ("ay", "ей"), ("oi", "ой"), ("oy", "ой"), ("au", "о"), ("aw", "о"), ("ew", "ью"),
    ("ya", "я"), ("yu", "ю"), ("yo", "йо"), ("ye", "е"),
    ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"),
    ("j", "дж"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"),
    ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "и"), ("z", "з"),
]


def by_rules(word: str) -> str:
    w = word.lower()
    w = re.sub(r"([bcdfgklmnprstvz])\1", r"\1", w)          # double consonants
    if len(w) > 3 and w.endswith("e") and w[-2] not in "aeiouy":
        w = w[:-1]                                            # silent final e: "make", "code"
    w = re.sub(r"c(?=[eiy])", "s", w)                         # "cell", "city"
    if w.startswith(("kn", "wr")):
        w = w[1:]
    out, i = [], 0
    while i < len(w):
        for chunk, ru in _RULES:
            if w.startswith(chunk, i):
                out.append(ru)
                i += len(chunk)
                break
        else:
            i += 1
    return "".join(out)


def spell(letters: str) -> str:
    return " ".join(LETTERS.get(ch, ch) for ch in letters.lower())


_CAMEL = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def word(token: str) -> str:
    """One Latin token (letters, maybe with digits/hyphens) -> Cyrillic for speech."""
    low = token.lower()
    if low in WORDS:
        return WORDS[low]
    if "-" in token:
        return " ".join(word(p) for p in token.split("-") if p)
    if any(ch.isdigit() for ch in token):       # "M2", "PS5", "4K", "X3D"
        return " ".join(_digits(p) if p.isdigit() else word(p) for p in re.findall(r"[A-Za-z]+|\d+", token))
    letters_only = re.sub(r"[^A-Za-z]", "", token)
    if not letters_only:
        return token
    has_vowel = bool(re.search(r"[aeiouy]", low))
    if token.isupper() and (len(letters_only) <= 3 or not has_vowel):
        return spell(letters_only)              # USB, GPU, SQL
    parts = _CAMEL.findall(token)
    if len(parts) > 1 and not token.isupper():  # GitHub, iPhone, PlayStation
        return " ".join(word(p) for p in parts)
    cmu = _load_cmu().get(low)
    if cmu:
        return from_phonemes(cmu.split())
    if token.isupper() and len(letters_only) <= 5:
        return spell(letters_only)              # NVME-like abbreviations
    return by_rules(low)


def _digits(digits: str) -> str:
    from num2words import num2words

    return num2words(int(digits), lang="ru")


def model_number(digits: str) -> str:
    """'3060' after RTX -> 'тридцать шестьдесят', '13700' -> 'тринадцать семьсот', '6600' stays a number."""
    from num2words import num2words

    if len(digits) == 4 and digits[2] != "0" and not digits.endswith("00") and digits[0] != "0":
        return f"{num2words(int(digits[:2]), lang='ru')} {num2words(int(digits[2:]), lang='ru')}"
    if len(digits) == 5 and digits[2] != "0" and digits[0] != "0":
        return f"{num2words(int(digits[:2]), lang='ru')} {num2words(int(digits[2:]), lang='ru')}"
    return num2words(int(digits), lang="ru")


def special(text: str) -> str:
    for pattern, repl in _SPECIAL:
        text = pattern.sub(repl, text)
    return text
