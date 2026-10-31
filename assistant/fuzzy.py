"""A spoken name against real names (tracks, files): which one the user meant, or a question if it is unclear.

The recognizer writes an English title in Russian letters ("трабл", "блэк мэджик") and mishears a sound or two.
Both sides are brought to one rough Latin sound form and to its consonant skeleton, so "трабл" meets "Trouble"
and "блэк мэджик" meets "Black Magic"; Russian titles are compared as they are too. A pick needs a clear winner:
two close candidates are asked about instead of guessing (the wrong song is worse than a question).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

_CYR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
        "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e",
        "ю": "yu", "я": "ya"}
# English spelling -> sound, roughly the way a Russian ear writes it down. Order matters.
_SOUND = [(r"dzh|dj|zh|j|g(?=[eiy])", "J"), (r"tch|ch", "C"), (r"sch|sh", "X"), (r"ph", "f"), (r"th", "t"),
          (r"kh", "h"), (r"ck|q|c(?![eiy])", "k"), (r"c", "s"), (r"x", "ks"), (r"w|v", "v"), (r"ts|tz", "Z"),
          (r"z", "s"), (r"ough", "o"), (r"ee|ea|ie|ey", "i"), (r"oo|ou", "u"), (r"y(?=[aeiou])", "")]
_VOWELS = re.compile(r"[aeiouy]")
# Parts of file names that are not the title: "(Official Video)", "[320 kbps]", "01 - ", "feat. …".
_JUNK = re.compile(r"\([^)]*\)|\[[^\]]*\]|\b(?:feat|ft|prod)\b\.?.*$|^\s*\d{1,3}\s*[-._)]\s*|\b(?:official|video|audio|"
                   r"lyrics?|remastered|hq|hd|mp3)\b", re.I)


def clean(name: str) -> str:
    """Lower case, no brackets or track numbers, letters and digits only."""
    name = _JUNK.sub(" ", name.replace("_", " ")).lower().replace("ё", "е").replace("'", "").replace("’", "")
    return " ".join(re.findall(r"[a-zа-я0-9]+", name))


def sound(text: str) -> str:
    """'трабл' and 'trouble' -> close sound forms ('trabl', 'trubli')."""
    # A silent final "e" of an English word ("temple") is not heard; "-dge", "-ge" sound as "дж" ("damage").
    words = [re.sub(r"(?<=[^aeiouy])e$", "", re.sub(r"d?ge$", "J", w)) if w.isascii() and len(w) > 3 else w
             for w in clean(text).split()]
    out = "".join(_CYR.get(ch, ch) for ch in " ".join(words))
    for pattern, repl in _SOUND:
        out = re.sub(pattern, repl, out)
    return re.sub(r"(.)\1+", r"\1", out).lower()


def skeleton(text: str) -> str:
    """Consonants only: vowels are what the ear gets wrong most ('vandir' / 'vondir')."""
    return " ".join(w for w in (_VOWELS.sub("", w) or w[:1] for w in sound(text).split()) if w)


def _pieces(name: str) -> list[str]:
    """'Artist - Title': the title alone is a name too (people say the title, not the artist)."""
    parts = [p for p in re.split(r"\s[-–—]\s|\s*[|•]\s*", name) if clean(p)]
    return [name, *parts[1:], parts[0]] if len(parts) > 1 else [name]


def _heard(query: str, name: str) -> float:
    q_plain, plain = clean(query), clean(name)
    if not q_plain or not plain:
        return 0.0
    s = max(fuzz.ratio(q_plain, plain), fuzz.token_sort_ratio(q_plain, plain))
    # The sound form decides; the skeleton backs it up (it alone would match too much: "trbl" is short).
    q_sound, snd, q_skel, skel = sound(query), sound(name), skeleton(query), skeleton(name)
    heard = 0.55 * max(fuzz.ratio(q_sound, snd), fuzz.token_sort_ratio(q_sound, snd)) + \
        0.45 * max(fuzz.ratio(q_skel, skel), fuzz.token_sort_ratio(q_skel, skel))
    return max(s, heard)


def score(query: str, name: str) -> float:
    """0..100: how well the spoken `query` fits the name."""
    n = len(clean(query).split())
    best = 0.0
    for piece in _pieces(name):
        best = max(best, _heard(query, piece))
        words = clean(piece).split()
        # "блэк мэджик" for "Black Magic Woman": whole words of a longer title, worth less the less was said.
        if 0 < n < len(words):
            cover = n / len(words)
            for i in range(len(words) - n + 1):
                best = max(best, _heard(query, " ".join(words[i:i + n])) * (0.8 + 0.2 * cover))
    return best


@dataclass
class Pick:
    best: str | None = None            # the name to use (a clear winner)
    score: float = 0.0
    close: list[str] = field(default_factory=list)   # candidates to ask about: a near tie or a doubtful best

    @property
    def unsure(self) -> bool:
        return self.best is None and bool(self.close)


def pick(query: str, names, sure: float = 78, maybe: float = 62, margin: float = 6) -> Pick:
    """The name the user meant. Clear winner -> best; two close ones or only a doubtful one -> close (ask)."""
    ranked = sorted(((score(query, n), n) for n in names), reverse=True)
    if not ranked or ranked[0][0] < maybe:
        return Pick()
    top, name = ranked[0]
    rivals = [n for s, n in ranked[1:3] if s >= maybe and top - s < margin and clean(n) != clean(name)]
    if top >= sure and not rivals:
        return Pick(name, top)
    return Pick(None, top, [name, *rivals])


_ORDINAL = {"перв": 0, "втор": 1, "трет": 2}


def choose(answer: str, options: list[str]) -> str | None:
    """The answer to "«A» или «B»?": "первую", "вторую", "да" (the first), or the name again."""
    words = clean(answer).split()
    for w in words:
        for stem, i in _ORDINAL.items():
            if w.startswith(stem) and i < len(options):
                return options[i]
    if words and words[0] in ("да", "давай", "ага", "угу", "конечно", "ее", "его", "включай", "включи"):
        if len(words) == 1 or len(options) == 1:
            return options[0]
    ranked = sorted(((score(answer, o), o) for o in options), reverse=True)
    if ranked and ranked[0][0] >= 60 and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 8):
        return ranked[0][1]
    return None
