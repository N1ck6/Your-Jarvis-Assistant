"""Solve a task: from the phrase itself, from the selected text or from a region of the screen.

The code only tells where the task is; the rest is a prompt skill (commands.toml): the cloud model solves it, the
answer is shown in two pinned windows, a short solution and the answer alone, as plain text.

"реши задачу с экрана" asks for a region; "реши эту задачу" right after "что на экране" reuses that region;
"реши выделенное" takes the selection; "реши задачу: два поезда вышли..." solves what was said.
Settings: [config] of skill.toml; the prompt: prompts/solve.md; checked phrases: tests/phrases.toml.
"""
from __future__ import annotations

import re

from assistant.skills.base import Intent, Skill

_TASK = r"(?:задач\w*|задани\w*|пример\w*|уравнени\w*|неравенств\w*|упражнени\w*|вопрос\w*|тест\w*|номер\w*|систем\w*)"
_THIS = r"(?:эт\w*|ее|его|их|вот эт\w*|вот|за меня|сам|мне)"
# What a maths task asks to find. Not "скорость" or "объем": "узнай скорость интернета" is not a task.
_UNKNOWN = (r"икс|x|игрек|y|неизвестн\w*|корни|корень уравнения|значение (?:выражения|функции)|производн\w*|"
            r"интеграл|предел|площадь|периметр")
_MATH_VERB = r"(?:найди|найти|определи|вычисли|чему (?:равен|равна|равно|равны))"
_SOLVE = re.compile(
    rf"^(?:а |теперь |давай |ну |тогда |и )*(?:"
    rf"(?:реши|решите|решить|решай|порешай|помоги решить|помоги с решением|можешь решить|сможешь решить)(?: (?P<rest>.*))?|"
    rf"(?:сделай|выполни|ответь на|дай ответ на|найди ответ на|подскажи ответ на) "
    rf"(?P<task>(?:{_THIS} )+{_TASK}.*|{_TASK}(?: (?:с|со|на|в|из) .*)?)|"
    # "найди икс из выражения ...", "чему равен икс", "вычисли производную ...": maths without the word "реши"
    rf"{_MATH_VERB} (?P<math>(?:{_UNKNOWN})(?: .*)?)|(?P<expr>(?:{_UNKNOWN}) из (?:выражени|уравнени|услови)\w*(?: .*)?)"
    rf")$")
_SCREEN = re.compile(r"\b(экран\w*|скрин\w*|монитор\w*|окн[оеа]|картинк\w*|фото\w*|изображени\w*)\b")
_SELECTED = re.compile(r"\b(выделен\w*|выделил\w*|выделени\w*)\b")
_POINTER = re.compile(rf"\b(?:{_THIS}|{_TASK})\b")
# Words that point at the task without stating it: "реши эту задачу за меня пожалуйста".
_FILLER = re.compile(rf"\b(?:{_THIS}|{_TASK}|{_UNKNOWN}|выражени\w*|услови\w*|с|со|на|в|из|по|и|у|меня|тут|здесь|там|пожалуйста|быстро|скорее|"
                     r"плиз|сейчас|которая|который|которое|написан\w*|есть|подробно|кратко|коротко)\b")


class SolveSkill(Skill):
    def match(self, text: str) -> Intent | None:
        m = _SOLVE.match(text)
        if not m:
            return None
        maths = m.group("math") or m.group("expr")
        rest = (m.group("rest") or m.group("task") or maths or "").strip()
        if rest and not maths and not any(p.search(rest) for p in (_POINTER, _SCREEN, _SELECTED)):
            return None  # "реши 2 плюс 2": plain arithmetic, the calculator answers it at once
        if _SCREEN.search(rest):
            source = "screen"
        elif _SELECTED.search(rest):
            source = "selection"
        elif len(_FILLER.sub(" ", rest).split()) >= 2:
            source = "said"   # the condition is in the phrase itself
        else:
            source = "auto"   # "реши эту задачу": the recent region, the selection or a new region
        return Intent(self.name, source, text=text)


def create() -> Skill:
    return SolveSkill()
