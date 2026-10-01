"""Instant answers without a model: arithmetic, percentages, units, days until a date.

"сколько будет 15% от 2300", "посчитай 128 умножить на 7", "корень из 144", "сколько дней до нового года",
"какой день будет через 100 дней", "100 миль в километрах", "30 градусов цельсия в фаренгейтах".
Currency conversion needs today's rate and goes to the internet answer instead.
"""
from __future__ import annotations

import ast
import datetime as dt
import math
import operator
import re

from assistant.nlu import plural, words_to_numbers
from assistant.skills.base import Intent, Reply, Skill, Tool

_NUM = r"-?\d+(?:[.,]\d+)?"
_ASK = r"^(?:сколько будет|посчитай|подсчитай|вычисли|реши|скажи сколько будет|а сколько будет)\s+"
_OPS = [(r"\s*(?:умножить на|умножь на|умноженное на|помножить на|x|х|×|\*)\s*", " * "),
        (r"\s*(?:разделить на|делить на|деленное на|поделить на|/|:)\s*", " / "),
        (r"\s*(?:плюс|\+|прибавить)\s*", " + "), (r"\s*(?:минус|отнять|−)\s*", " - "),
        (r"\s*в степени\s*", " ** "), (r"\s*в квадрате", " ** 2"), (r"\s*в кубе", " ** 3")]
_PERCENT_OF = re.compile(rf"(?P<p>{_NUM})\s*(?:%|процент\w*)\s+от\s+(?P<x>{_NUM})")
_ROOT = re.compile(rf"(?:квадратный\s+)?корень\s+(?:квадратный\s+)?из\s+(?P<x>{_NUM})")
_ALLOWED = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
            ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}

# spoken stem -> (kind, factor to the base unit)
_UNITS = {
    "километр": ("len", 1000.0), "км": ("len", 1000.0), "метр": ("len", 1.0), "сантиметр": ("len", 0.01),
    "миллиметр": ("len", 0.001), "мил": ("len", 1609.344), "фут": ("len", 0.3048), "дюйм": ("len", 0.0254),
    "ярд": ("len", 0.9144), "килограмм": ("mass", 1.0), "кг": ("mass", 1.0), "грамм": ("mass", 0.001),
    "фунт": ("mass", 0.45359237), "унци": ("mass", 0.028349523), "тонн": ("mass", 1000.0),
    "литр": ("vol", 1.0), "миллилитр": ("vol", 0.001), "галлон": ("vol", 3.785411784),
    "цельси": ("temp", 0.0), "фаренгейт": ("temp", 0.0), "кельвин": ("temp", 0.0),
}
_FORMS = {"километр": ("километр", "километра", "километров"), "км": ("километр", "километра", "километров"),
          "метр": ("метр", "метра", "метров"), "сантиметр": ("сантиметр", "сантиметра", "сантиметров"),
          "миллиметр": ("миллиметр", "миллиметра", "миллиметров"), "мил": ("миля", "мили", "миль"),
          "фут": ("фут", "фута", "футов"), "дюйм": ("дюйм", "дюйма", "дюймов"), "ярд": ("ярд", "ярда", "ярдов"),
          "килограмм": ("килограмм", "килограмма", "килограммов"), "кг": ("килограмм", "килограмма", "килограммов"),
          "грамм": ("грамм", "грамма", "граммов"), "фунт": ("фунт", "фунта", "фунтов"), "унци": ("унция", "унции", "унций"),
          "тонн": ("тонна", "тонны", "тонн"), "литр": ("литр", "литра", "литров"),
          "миллилитр": ("миллилитр", "миллилитра", "миллилитров"), "галлон": ("галлон", "галлона", "галлонов"),
          "цельси": ("градус Цельсия", "градуса Цельсия", "градусов Цельсия"),
          "фаренгейт": ("градус Фаренгейта", "градуса Фаренгейта", "градусов Фаренгейта"),
          "кельвин": ("кельвин", "кельвина", "кельвинов")}
_CONVERT = re.compile(rf"^(?:сколько\s+(?:будет\s+)?|переведи\s+|конвертируй\s+)?(?P<x>{_NUM})\s+(?:градус\w*\s+)?(?P<a>[а-яё]+)"
                      rf"\s+(?:в|во)\s+(?:градус\w*\s+)?(?P<b>[а-яё]+)$")
_DAYS_TO = re.compile(r"^(?:сколько\s+)?(?:дней|осталось дней|дней осталось)\s+до\s+(?P<what>.+)$|"
                      r"^сколько осталось до\s+(?P<what2>.+)$")
_IN_DAYS = re.compile(r"^(?:какое число|какой день|какая дата)\s+будет\s+через\s+(?P<n>\d+)\s+(?:дн|день|дня|дней)\w*$")
_HOLIDAYS = {"нового года": (1, 1), "новый год": (1, 1), "рождества": (1, 7), "восьмого марта": (3, 8),
             "8 марта": (3, 8), "23 февраля": (2, 23), "дня победы": (5, 9), "9 мая": (5, 9), "1 мая": (5, 1),
             "лета": (6, 1), "осени": (9, 1), "зимы": (12, 1), "весны": (3, 1), "дня рождения": None}
_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
           "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_WEEKDAY_STEMS = ["понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень"]


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def fmt(x: float) -> str:
    """12.0 -> '12', 3.14159 -> '3,14', 1234567 -> '1 234 567'."""
    if abs(x - round(x)) < 1e-9:
        return f"{int(round(x)):,}".replace(",", " ")
    digits = 2 if abs(x) >= 1 else 4
    text = f"{x:,.{digits}f}".rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",")


def safe_eval(expr: str) -> float:
    """+ - * / ** on numbers only: the text never reaches Python's eval."""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("слишком большая степень")
            return _ALLOWED[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED:
            return _ALLOWED[type(node.op)](ev(node.operand))
        raise ValueError("не арифметика")
    return ev(ast.parse(expr, mode="eval"))


def arithmetic(text: str) -> float | None:
    """'15 процентов от 2300' / '128 умножить на 7' / 'корень из 144' -> the number."""
    t = re.sub(_ASK, "", words_to_numbers(text)).strip(" ?=")
    if m := _PERCENT_OF.fullmatch(t):
        return _num(m.group("p")) * _num(m.group("x")) / 100
    if m := _ROOT.fullmatch(t):
        return math.sqrt(_num(m.group("x")))
    expr = t
    for pattern, op in _OPS:
        expr = re.sub(pattern, op, expr)
    expr = re.sub(r"(?<=\d),(?=\d)", ".", expr)
    if not re.fullmatch(r"[\d\s.+\-*/()]+", expr) or not re.search(r"\d\s*(?:[-+/]|\*\*?)\s*\(?\s*-?\d", expr):
        return None
    try:
        return safe_eval(expr)
    except (ValueError, ZeroDivisionError, SyntaxError, OverflowError):
        return None


def _unit(word: str) -> str | None:
    return next((stem for stem in sorted(_UNITS, key=len, reverse=True) if word.startswith(stem)), None)


def convert(x: float, a: str, b: str) -> float | None:
    ua, ub = _unit(a), _unit(b)
    if not ua or not ub or _UNITS[ua][0] != _UNITS[ub][0]:
        return None
    if _UNITS[ua][0] == "temp":
        c = x if ua == "цельси" else (x - 32) * 5 / 9 if ua == "фаренгейт" else x - 273.15
        return c if ub == "цельси" else c * 9 / 5 + 32 if ub == "фаренгейт" else c + 273.15
    return x * _UNITS[ua][1] / _UNITS[ub][1]


def target_date(what: str, today: dt.date) -> dt.date | None:
    what = words_to_numbers(what.strip(" ?"))
    if _HOLIDAYS.get(what):
        month, day = _HOLIDAYS[what]
    elif m := re.fullmatch(r"(\d{1,2})(?:-?го)?\s+(" + "|".join(_MONTHS) + r")", what):
        month, day = _MONTHS[m.group(2)], int(m.group(1))
    else:
        for i, stem in enumerate(_WEEKDAY_STEMS):
            if what.startswith(stem):
                return today + dt.timedelta(days=(i - today.weekday()) % 7 or 7)
        return None
    try:
        date = dt.date(today.year, month, day)
    except ValueError:
        return None
    return date if date > today else date.replace(year=today.year + 1)


class CalcSkill(Skill):
    name = "calc"
    title = "Калькулятор и даты"
    examples = [
        'сколько будет <выражение>',
        '<N> процентов от <M>',
        'сколько дней до <дата или праздник>',
        '<N> <единица> в <единица> (мили в километры, фунты в кг, цельсий в фаренгейт)',
    ]

    def match(self, text: str) -> Intent | None:
        if (m := _DAYS_TO.match(text)) and target_date(m.group("what") or m.group("what2"), dt.date.today()):
            return Intent(self.name, "days_to", {"what": m.group("what") or m.group("what2")}, text)
        if m := _IN_DAYS.match(text):
            return Intent(self.name, "in_days", {"n": int(m.group("n"))}, text)
        if (m := _CONVERT.match(words_to_numbers(text))) and _unit(m.group("a")) and _unit(m.group("b")):
            return Intent(self.name, "convert", {"x": m.group("x"), "a": m.group("a"), "b": m.group("b")}, text)
        if arithmetic(text) is not None:
            return Intent(self.name, "calc", {"expr": text}, text)
        return None

    def tools(self) -> list[Tool]:
        return [Tool("calculate", "Точно посчитать арифметическое выражение или проценты (вместо подсчёта в уме).",
                     {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}, "calc")]

    async def handle(self, intent: Intent) -> Reply:
        today = dt.date.today()
        if intent.action == "days_to":
            what = str(intent.slots["what"])
            date = target_date(what, today)
            if date is None:
                return Reply("Не понял дату.", fallthrough=True)
            n = (date - today).days
            return Reply(f"До {what} {n} {plural(n, 'день', 'дня', 'дней')}.", listen_after=False)
        if intent.action == "in_days":
            date = today + dt.timedelta(days=int(intent.slots["n"]))
            months = list(_MONTHS)
            return Reply(f"{date.day} {months[date.month - 1]} {date.year} года, {_WEEKDAYS[date.weekday()]}.",
                         listen_after=False)
        if intent.action == "convert":
            value = convert(_num(intent.slots["x"]), intent.slots["a"], intent.slots["b"])
            if value is None:
                return Reply("Эти единицы не переводятся друг в друга.")
            forms = _FORMS[_unit(intent.slots["b"])]
            noun = plural(int(value), *forms) if abs(value - round(value)) < 1e-9 else forms[1]
            return Reply(f"{fmt(value)} {noun}.", tool_result=fmt(value), listen_after=False)
        value = arithmetic(str(intent.slots.get("expr") or ""))
        if value is None:
            return Reply("Не смог посчитать.", tool_result="не арифметика", fallthrough=True)
        return Reply(f"{fmt(value)}.", tool_result=fmt(value), listen_after=False)


def create() -> Skill:
    return CalcSkill()
