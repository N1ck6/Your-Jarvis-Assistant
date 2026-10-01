"""Formulas the models write in LaTeX ($x^2$, \\frac{a}{b}): spoken as words, shown as plain Unicode.

Without this the voice reads "доллар экс домик два доллар" and the card shows raw backslashes.
"""
from __future__ import annotations

import re
from typing import Callable

_GREEK = {
    "alpha": ("альфа", "α"), "beta": ("бета", "β"), "gamma": ("гамма", "γ"), "delta": ("дельта", "δ"),
    "epsilon": ("эпсилон", "ε"), "varepsilon": ("эпсилон", "ε"), "zeta": ("дзета", "ζ"), "eta": ("эта", "η"),
    "theta": ("тета", "θ"), "lambda": ("лямбда", "λ"), "mu": ("мю", "μ"), "nu": ("ню", "ν"), "xi": ("кси", "ξ"),
    "pi": ("пи", "π"), "rho": ("ро", "ρ"), "sigma": ("сигма", "σ"), "tau": ("тау", "τ"), "phi": ("фи", "φ"),
    "varphi": ("фи", "φ"), "chi": ("хи", "χ"), "psi": ("пси", "ψ"), "omega": ("омега", "ω"),
    "Gamma": ("гамма", "Γ"), "Delta": ("дельта", "Δ"), "Theta": ("тета", "Θ"), "Lambda": ("лямбда", "Λ"),
    "Sigma": ("сигма", "Σ"), "Phi": ("фи", "Φ"), "Psi": ("пси", "Ψ"), "Omega": ("омега", "Ω"), "Pi": ("пи", "Π"),
}
# command -> (speech, display)
_COMMANDS = {
    "cdot": (" умножить на ", "·"), "times": (" умножить на ", "×"), "div": (" делить на ", "÷"),
    "pm": (" плюс минус ", "±"), "mp": (" минус плюс ", "∓"), "leq": (" меньше или равно ", "≤"),
    "le": (" меньше или равно ", "≤"), "geq": (" больше или равно ", "≥"), "ge": (" больше или равно ", "≥"),
    "neq": (" не равно ", "≠"), "ne": (" не равно ", "≠"), "approx": (" примерно равно ", "≈"),
    "equiv": (" тождественно равно ", "≡"), "sim": (" порядка ", "~"), "to": (" стремится к ", "→"),
    "rightarrow": (" следует ", "→"), "Rightarrow": (" следовательно ", "⇒"), "leftrightarrow": (" равносильно ", "↔"),
    "infty": (" бесконечность ", "∞"), "lim": (" предел ", "lim"), "sum": (" сумма ", "Σ"), "prod": (" произведение ", "Π"),
    "int": (" интеграл ", "∫"), "oint": (" интеграл ", "∮"), "partial": (" частная производная ", "∂"),
    "nabla": (" набла ", "∇"), "sin": (" синус ", "sin"), "cos": (" косинус ", "cos"), "tan": (" тангенс ", "tg"),
    "tg": (" тангенс ", "tg"), "cot": (" котангенс ", "ctg"), "ctg": (" котангенс ", "ctg"),
    "arcsin": (" арксинус ", "arcsin"), "arccos": (" арккосинус ", "arccos"), "arctan": (" арктангенс ", "arctg"),
    "ln": (" натуральный логарифм ", "ln"), "log": (" логарифм ", "log"), "lg": (" десятичный логарифм ", "lg"),
    "exp": (" экспонента ", "exp"), "max": (" максимум ", "max"), "min": (" минимум ", "min"),
    "in": (" принадлежит ", "∈"), "notin": (" не принадлежит ", "∉"), "subset": (" подмножество ", "⊂"),
    "cup": (" объединение ", "∪"), "cap": (" пересечение ", "∩"), "forall": (" для любого ", "∀"),
    "exists": (" существует ", "∃"), "circ": (" градусов ", "°"), "degree": (" градусов ", "°"),
    "cdots": (" и так далее ", "…"), "ldots": (" и так далее ", "…"), "dots": (" и так далее ", "…"),
    "angle": (" угол ", "∠"), "perp": (" перпендикулярно ", "⊥"), "parallel": (" параллельно ", "∥"),
    "Delta": (" дельта ", "Δ"), "prime": (" штрих ", "′"),
}
_DROP = re.compile(r"\\(?:left|right|big|Big|bigg|Bigg|displaystyle|limits|nolimits|,|;|:|!|quad|qquad)(?![A-Za-z])|\\ ")
_WRAP = re.compile(r"\\(?:text|mathrm|mathbf|mathit|mathbb|mathcal|operatorname|textbf|boldsymbol|vec|overline|hat)"
                   r"\s*\{([^{}]*)\}")
# Latin letters as Russian maths reads them.
_LETTERS = {"a": "а", "b": "бэ", "c": "цэ", "d": "дэ", "e": "е", "f": "эф", "g": "жэ", "h": "аш", "i": "и", "j": "йот",
            "k": "ка", "l": "эль", "m": "эм", "n": "эн", "o": "о", "p": "пэ", "q": "ку", "r": "эр", "s": "эс", "t": "тэ",
            "u": "у", "v": "вэ", "w": "дубль-вэ", "x": "икс", "y": "игрек", "z": "зет"}
_SUP = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUB = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")

_SPANS = [
    re.compile(r"\$\$(.+?)\$\$", re.S),
    re.compile(r"\\\[(.+?)\\\]", re.S),
    re.compile(r"\\\((.+?)\\\)", re.S),
    re.compile(r"(?<![\w$])\$(?=\S)([^$\n]{1,300}?)(?<=\S)\$(?![\w$])"),
]
_MATHY = re.compile(r"[\\^_={}]|(?<![A-Za-zА-Яа-яЁё])[a-zA-Z](?![A-Za-z])")
_COMMAND = re.compile(r"\\([A-Za-z]+)")


def _replace_spans(text: str, convert: Callable[[str], str]) -> str:
    for pattern in _SPANS:
        def repl(m: re.Match[str]) -> str:
            inner = m.group(1)
            if pattern is _SPANS[-1] and not _MATHY.search(inner):
                return m.group(0)  # "$5 и $" is money, not a formula
            return convert(inner)
        text = pattern.sub(repl, text)
    if _COMMAND.search(text) and re.search(r"\\(?:frac|sqrt|cdot|times|lim|sum|int|pi|alpha|beta|infty|to|leq|geq|neq|"
                                           r"approx|pm|sin|cos|log|ln|left|right|text|mathrm)\b", text):
        text = convert(text)  # bare LaTeX without $ around it
    return text


def _innermost(text: str, rules: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str] | str]]) -> str:
    """Applies the rules until nothing changes: nested braces are resolved from the inside out."""
    for _ in range(12):
        before = text
        for pattern, repl in rules:
            text = pattern.sub(repl, text)
        if text == before:
            break
    return text


# ---------------------------------------------------------------- speech
_FRAC = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT_N = re.compile(r"\\sqrt\s*\[([^\]]*)\]\s*\{([^{}]*)\}")
_SQRT = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_LIM = re.compile(r"\\lim\s*_\s*\{([^{}]*)\}")
_POW = re.compile(r"\^\s*(?:\{([^{}]*)\}|(\\[A-Za-z]+|[A-Za-z0-9]))")
_INDEX = re.compile(r"_\s*(?:\{([^{}]*)\}|(\\[A-Za-z]+|[A-Za-z0-9]))")
_GROUP = re.compile(r"\{([^{}]*)\}")


def _lim_words(m: re.Match[str]) -> str:
    cond = m.group(1).replace("\\infty", " бесконечности ")
    cond = re.sub(r"\\to\s*0(?![\d.,])", " стремящемся к нулю ", cond).replace("\\to", " стремящемся к ")
    return f" предел при {cond} "


def _power_words(m: re.Match[str]) -> str:
    p = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
    if p == "2":
        return " в квадрате "
    if p == "3":
        return " в кубе "
    return f" в степени {p} "


def _speech(latex: str) -> str:
    t = _DROP.sub(" ", latex)
    t = _innermost(t, [(_WRAP, r"\1")])
    t = _innermost(t, [
        (_LIM, _lim_words),
        (_FRAC, r" \1 делить на \2 "),
        (_SQRT_N, r" корень степени \1 из \2 "),
        (_SQRT, r" корень из \1 "),
        (_POW, _power_words),
        (_INDEX, lambda m: " " + (m.group(1) if m.group(1) is not None else m.group(2)).replace(",", " и ") + " "),
    ])
    t = _innermost(t, [(_GROUP, r" \1 ")])  # plain groups last: "\frac{-b + \sqrt{D}}{2a}" needs its braces

    def command(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in _COMMANDS:
            return _COMMANDS[name][0]
        if name in _GREEK:
            return f" {_GREEK[name][0]} "
        return " "
    t = _COMMAND.sub(command, t)
    t = re.sub(r"(?<![A-Za-z])([fgh])\s*'\s*\(", r"\1 штрих от (", t)
    t = re.sub(r"(?<![A-Za-z])([fgh])\s*\(", r"\1 от (", t)
    t = t.replace("'", " штрих ")
    t = re.sub(r"\s-\s|(?<=[\s(])-(?=\w)", " минус ", f" {t} ")
    t = re.sub(r"(?<=\w)\s*-\s*(?=\w)", " минус ", t)
    t = t.replace("*", " умножить на ").replace("/", " делить на ").replace("<", " меньше ").replace(">", " больше ")
    t = re.sub(r"(\d)([a-zA-Z])", r"\1 \2", t)                                            # "4ac" -> "4 ac"
    t = re.sub(r"(?<![A-Za-z])[a-zA-Z]{2,4}(?![A-Za-z])", lambda m: " ".join(m.group(0)), t)  # "mc" is m times c
    t = re.sub(r"(?<![A-Za-z])([a-zA-Z])(?![A-Za-z])", lambda m: f" {_LETTERS.get(m.group(1).lower(), m.group(1))} ", t)
    t = t.replace("(", " ").replace(")", " ").replace("[", " ").replace("]", " ").replace("|", " модуль ")
    return re.sub(r"\s+", " ", t).strip()


def to_speech(text: str) -> str:
    """'Формула $y = x^2$' -> 'Формула игрек = икс в квадрате' (the normalizer then says "равно")."""
    if "$" not in text and "\\" not in text:
        return text
    return _replace_spans(text, lambda inner: f" {_speech(inner)} ")


# ---------------------------------------------------------------- display
def _sup(s: str) -> str:
    s = s.strip()
    return s.translate(_SUP) if all(ch in "0123456789+-=()n" for ch in s) else f"^({s})" if len(s) > 1 else f"^{s}"


def _sub(s: str) -> str:
    s = s.strip()
    return s.translate(_SUB) if all(ch in "0123456789+-=()," for ch in s) else f"_{s}"


def _paren(s: str) -> str:
    s = s.strip()
    return f"({s})" if re.search(r"[+\-−·×/ ]", s) else s


def _display(latex: str) -> str:
    t = _DROP.sub(" ", latex)
    t = _innermost(t, [(_WRAP, r"\1")])

    def command(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in _COMMANDS:
            return _COMMANDS[name][1]
        if name in _GREEK:
            return _GREEK[name][1]
        return m.group(0)
    t = _innermost(t, [
        (_LIM, lambda m: "lim(" + m.group(1).replace("\\to", "→").replace("\\infty", "∞").strip() + ") "),
        (_FRAC, lambda m: f"{_paren(m.group(1))}/{_paren(m.group(2))}"),
        (_SQRT_N, lambda m: f"{_sup(m.group(1))}√{_paren(m.group(2))}"),
        (_SQRT, lambda m: f"√{_paren(m.group(1))}"),
        (_POW, lambda m: _sup(m.group(1) if m.group(1) is not None else m.group(2))),
        (_INDEX, lambda m: _sub(m.group(1) if m.group(1) is not None else m.group(2))),
    ])
    t = _innermost(t, [(_GROUP, r"\1")])  # plain groups last, after \frac and \sqrt took their own braces
    t = re.sub(r"\\([A-Za-z]+)\s?", lambda m: command(m) + ("" if m.group(1) in _GREEK else " "), t)
    t = t.replace("\\", "")
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def to_display(text: str) -> str:
    """'$x^2 + \\frac{1}{x}$' -> 'x² + 1/x' for cards and the chat."""
    if "$" not in text and "\\" not in text:
        return text
    return _replace_spans(text, _display)
