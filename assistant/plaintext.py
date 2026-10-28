"""Model answers as plain text for windows and the voice: no markdown, LaTeX, wrapping brackets or "Ответ:".

Used by prompt skills (assistant/prompting.py), e.g. the solution and the answer of "реши задачу" in two windows.
"""
from __future__ import annotations

import re

from assistant import mathtext

_HEAD = re.compile(r"^\W*(решение|ответ|answer|solution)\W*?:?\s*", re.I)
_MD_LINE = re.compile(r"^\s*(?:#{1,6}\s*|>\s*|[-*•+]\s+|```\w*\s*$)")
_EMPH = re.compile(r"(\*\*|__|~~|`)")
_STAR = re.compile(r"(?<=[\w)])\s*\*\s*(?=[\w(])")
_SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
_WRAPPERS = [("(", ")"), ("[", "]"), ("{", "}"), ("«", "»"), ('"', '"'), ("'", "'"), ("“", "”")]


def split_solution(raw: str) -> tuple[str, str]:
    """'РЕШЕНИЕ: ... ОТВЕТ: ...' -> (solution, answer), both cleaned. A reply without the headers: the last line is
    the answer."""
    text = raw.replace("\r", "")
    m = re.search(r"(?im)^\W*ответ\W*?:?\s*", text)
    if m:
        solution, answer = text[:m.start()], text[m.end():]
    else:
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        solution, answer = "\n".join(lines[:-1]), (lines[-1] if lines else "")
    solution = _HEAD.sub("", plain(solution).strip(), count=1).strip()
    answer = clean_answer(answer)
    return solution, answer


def plain(text: str) -> str:
    """Markdown and LaTeX -> plain lines: '**Шаг 1.** $x^2 = 4$' -> 'Шаг 1. x² = 4'."""
    text = re.sub(r"\\boxed\s*\{([^{}]*)\}", r"\1", text)
    text = mathtext.to_display(text)
    lines = []
    for line in text.splitlines():
        line = _MD_LINE.sub("", line)
        if re.fullmatch(r"\s*(?:```|---+|\*\*\*+|___+)\s*", line):
            continue
        line = _EMPH.sub("", line)
        line = _STAR.sub(" · ", line)
        line = re.sub(r"(?<!\w)\*(\S[^*]*?)\*(?!\w)", r"\1", line)  # *курсив*
        line = line.replace("$", "").replace("\\", "")
        line = re.sub(r"\^(\d+)", lambda m: m.group(1).translate(_SUP), line)  # "x^2" -> "x²"
        lines.append(re.sub(r"[ \t]{2,}", " ", line).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def clean_answer(text: str) -> str:
    """The answer alone, one line: no 'Ответ:', no wrapping brackets or quotes, no final period."""
    text = plain(text)
    text = " ".join(ln.strip() for ln in text.splitlines() if ln.strip())
    text = _HEAD.sub("", text).strip()
    for _ in range(3):
        stripped = text.rstrip(".").strip()
        for left, right in _WRAPPERS:
            # "(2; 3)" is a point, kept; "[42]" or "«Б»" around the whole answer is a wrapper
            if (stripped.startswith(left) and stripped.endswith(right) and len(stripped) > 2
                    and not (left == "(" and re.search(r"[;,]", stripped))
                    and stripped.count(left) == 1 and stripped.count(right) == 1):
                stripped = stripped[1:-1].strip()
        if stripped == text:
            break
        text = stripped
    return text


_SAY = [("²", " в квадрате"), ("³", " в кубе"), ("√", " корень из "), ("π", " пи "), ("·", " умножить на "),
        ("×", " умножить на "), ("÷", " делить на "), ("≤", " меньше или равно "), ("≥", " больше или равно "),
        ("≠", " не равно "), ("≈", " примерно "), ("±", " плюс минус "), ("∞", " бесконечность "), ("°", " градусов ")]


def spoken(answer: str) -> str:
    """'x² = 4; x = ±2' -> words the voice can say."""
    t = answer
    for sym, word in _SAY:
        t = t.replace(sym, word)
    t = re.sub(r"(?<=\d)\s*/\s*(?=\d)", " делить на ", t)
    t = re.sub(r"\s*;\s*", ", ", t)
    return re.sub(r"\s+", " ", t).strip()
