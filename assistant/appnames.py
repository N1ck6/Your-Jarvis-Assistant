"""Spoken names of programs and games -> their real names ("дискорд" -> "discord", "вэ эс код" -> "visual studio code").

Speech recognition writes English names in Russian letters; the Start menu, window titles and the volume mixer use
the English ones. The table: config/app_names.toml (built in) + data/app_names.toml (the user's, wins) + the old
[apps] translit setting. Used by «открой …» (apps), «закрой / переключись на …» (windows) and «сделай … тише» (media).
"""
from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path

from rapidfuzz import fuzz, process

from assistant.paths import CONFIG_DIR, DATA_DIR

log = logging.getLogger("apps")

BUILTIN = CONFIG_DIR / "app_names.toml"
USER = DATA_DIR / "app_names.toml"

CYR2LAT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})

# English names read the Russian way, to compare with what was heard: "discord" -> "дискорд", "steam" -> "стим".
_LAT2CYR = [
    ("sch", "ш"), ("tch", "ч"), ("ght", "т"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("ph", "ф"), ("th", "т"),
    ("kh", "х"), ("ck", "к"), ("qu", "кв"), ("oo", "у"), ("ee", "и"), ("ea", "и"), ("ou", "ау"), ("ow", "оу"),
    ("ay", "ей"), ("ey", "ей"), ("oy", "ой"), ("ai", "ей"), ("au", "о"), ("ya", "я"), ("yu", "ю"), ("yo", "е"),
    ("ts", "ц"), ("x", "кс"), ("j", "дж"), ("w", "в"), ("q", "к"),
    ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"),
    ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"),
    ("u", "у"), ("v", "в"), ("y", "и"), ("z", "з"),
]
_LAT2CYR_RE = re.compile("|".join(re.escape(a) for a, _ in _LAT2CYR))
_LAT2CYR_MAP = dict(_LAT2CYR)

_cache: tuple[tuple, dict[str, str]] | None = None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _load(path: Path) -> dict[str, str]:
    try:
        with path.open("rb") as f:
            names = tomllib.load(f).get("names", {})
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("Словарь названий %s не прочитан: %s", path.name, exc)
        return {}
    return {_key(k): str(v).lower() for k, v in names.items() if isinstance(v, str)}


def _key(spoken: str) -> str:
    return re.sub(r"\s+", " ", spoken.lower().replace("ё", "е")).strip()


def table(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The whole table, re-read when a file changes (the user may edit data/app_names.toml while Jarvis runs)."""
    global _cache
    stamp = (_mtime(BUILTIN), _mtime(USER), tuple(sorted((extra or {}).items())))
    if _cache is None or _cache[0] != stamp:
        merged = _load(BUILTIN)
        merged.update({_key(k): v.lower() for k, v in (extra or {}).items()})
        merged.update(_load(USER))
        _cache = (stamp, merged)
    return _cache[1]


def to_cyrillic(name: str) -> str:
    """"discord" -> "дискорд", "telegram" -> "телеграм": how a Russian speaker would say an English name."""
    low = name.lower()
    low = re.sub(r"e\b", "", low) if len(low) > 4 else low      # silent final e: "chrome" -> "chrom"
    return _LAT2CYR_RE.sub(lambda m: _LAT2CYR_MAP[m.group(0)], low)


def variants(spoken: str, extra: dict[str, str] | None = None) -> list[str]:
    """What the spoken name may be called on the computer, best guess first:
    the table (exact, then a near miss: "дискорт"), word by word ("закрой окно стим чата"), transliteration."""
    low = _key(spoken)
    names = table(extra)
    out = [low]
    if low in names:
        out.append(names[low])
    else:
        hit = process.extractOne(low, names.keys(), scorer=fuzz.ratio, score_cutoff=85)
        if hit:
            out.append(names[hit[0]])
        words = low.split()
        if len(words) > 1:
            out.append(" ".join(names.get(w, w) for w in words))
    out.append(low.translate(CYR2LAT))
    return [v for v in dict.fromkeys(out) if v]
