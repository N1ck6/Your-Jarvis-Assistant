"""Solve a task: from the phrase itself, from the selected text or from a region of the screen.

The code only tells where the task is; the rest is a prompt skill (commands.toml): the cloud model solves it, the
answer is shown in two pinned windows, a short solution and the answer alone, as plain text.

"реши задачу с экрана" asks for a region; "реши эту задачу" right after "что на экране" reuses that region;
"реши выделенное" takes the selection; "реши задачу: два поезда вышли..." solves what was said.
After a solution, follow-ups work on the same task (followup): "а если скорость 80?", "то же самое, но для пяти",
"реши заново", "проверь" solve it again with the change; "почему так?", "объясни решение" explain it step by step.
Settings: [config] of skill.toml; the prompt: prompts/solve.md; checked phrases: tests/phrases.toml.
"""
from __future__ import annotations

import re
import time

from assistant.skills.base import Intent, Skill

_TASK = r"(?:задач\w*|задани\w*|домашк\w*|домашн\w* задани\w*|дз|пример\w*|уравнени\w*|неравенств\w*|упражнени\w*|вопрос\w*|тест\w*|номер\w*|систем\w*)"
_THIS = r"(?:эт\w*|ее|его|их|вот эт\w*|вот|за меня|сам|мне)"
# What a maths task asks to find. Not "скорость" or "объем": "узнай скорость интернета" is not a task.
_UNKNOWN = (r"икс|x|игрек|y|неизвестн\w*|корни|корень уравнения|значение (?:выражения|функции)|производн\w*|"
            r"интеграл|предел|площадь|периметр")
_MATH_VERB = r"(?:найди|найти|определи|вычисли|чему (?:равен|равна|равно|равны))"
_SOLVE = re.compile(
    rf"^(?:а |теперь |давай |ну |тогда |и )*(?:"
    rf"(?:реши|решите|решить|решай|порешай|помоги решить|помоги с решением|можешь решить|сможешь решить)(?: (?P<rest>.*))?|"
    rf"помоги(?: мне)? (?:с|со) (?P<help>{_TASK}.*)|"
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

# Follow-ups to the last solution (only right after it, see followup).
_LEAD = r"^(?:а |и |ну |тогда |хорошо |ладно |окей |так |слушай )*"
_AGAIN = re.compile(
    _LEAD + r"(?:(?:реши|перереши|пересчитай|посчитай|сделай|реши задачу)(?: (?:ее|его|это|эту|задачу|заново|снова|"
    r"еще раз|опять|по-другому|по другому|иначе|внимательно|внимательнее|нормально|правильно|по новой))+|перереши|"
    r"пересчитай)$")
_CHECK = re.compile(
    _LEAD + r"(?:(?:проверь|перепроверь)(?: (?:ее|его|это|решение|ответ|себя|задачу|еще раз))*|"
    r"(?:это |ответ |решение )?(?:неправильн\w*|неверн\w*|не сходится|не правильн\w*)(?: (?:ответ|решение|решил|посчитал))*|"
    r"(?:ты )?(?:ошибся|ошибаешься|неправильно решил|неправильно посчитал)(?: в (?:решении|ответе|расчетах))?|"
    r"(?:в ответе|в учебнике|в книге|правильный ответ) (?:другой|другое|не так|.+)|не тот ответ)$")
_EXPLAIN = re.compile(
    _LEAD + r"(?:почему(?: (?:так|такой ответ|это так|именно так|получилось так))?|"
    r"(?:объясни|поясни|распиши|расскажи)(?: (?:решение|подробнее|подробно|ход решения|по шагам|как решал|как ты решил|"
    r"как ты это решил|как решать|как получилось|откуда ответ|мне|это|еще раз|как|понятнее|проще))*|"
    r"как (?:ты )?(?:это )?(?:получил|получилось|вышло|решил|нашел|посчитал)(?: (?:ответ|это|такой ответ))?|"
    r"откуда (?:такой )?(?:ответ|это|число)(?: (?:взялся|взялось))?|покажи (?:ход )?решение|подробнее|"
    r"не понял (?:решение|как решал|откуда ответ))$")
# A change of the condition: "а если …", "а что если …", "то же самое, но …", "а для пяти рабочих", "пусть x = 3".
# "а в …", "а для …" only with a number or a quantity: "а в Сочи" after a solution is not about the task.
_REVISE = re.compile(
    _LEAD + r"(?:(?:что )?если|(?:то же самое|тоже самое|то же|так же|точно так же)(?: но| только)?|теперь|давай|"
    r"измени|поменяй|замени|пусть|допустим|предположим|представь что|добавь|убери|учти|без|только|но|"
    r"увеличь|уменьши|удвой|возьми|"
    r"(?:при|для|с|со|в|на|когда|у|за)(?= .*(?:\d|одн|два|двух|двое|три|трех|трое|четыр|пят|шест|сем|восем|девят|"
    r"десят|сорок|сто|двест|тысяч|половин|трет|вдвое|втрое|скорост|врем|раз|цен|колич|длин|ширин|высот|масс|объем|площад|"
    r"процент|человек|рабоч|час|минут|метр|километр|рубл|литр|грамм|икс|игрек|\bx\b|\by\b)))(?: |$)")


class SolveSkill(Skill):
    # "а если скорость будет восемьдесят километров в час" is longer than a usual follow-up; "неправильно" (system),
    # "объясни" (selection, explain), "а если x = 5" (calc) are about the solution right after it.
    long_followups = True
    followup_over = ("system", "explain", "selection", "calc")

    def match(self, text: str) -> Intent | None:
        m = _SOLVE.match(text)
        if not m:
            return None
        maths = m.group("math") or m.group("expr")
        rest = (m.group("rest") or m.group("task") or m.group("help") or maths or "").strip()
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

    def followup(self, text: str, last: Intent) -> Intent | None:
        run = getattr(self, "last_run", None)
        ttl = getattr(getattr(self.ctx, "config", None), "followup_sec", 900)
        if not run or not run.get("answer") or time.monotonic() - run["at"] > ttl:
            return None
        if _EXPLAIN.match(text):
            return Intent(self.name, "explain", text=text)
        if _AGAIN.match(text):
            return Intent(self.name, "revise", {"change": ""}, text)
        if _CHECK.match(text):
            return Intent(self.name, "revise", {"change": f"пользователь говорит: «{text}» — проверь решение"}, text)
        if _REVISE.match(text):
            other = self.app.brain.match_skill(text)
            if other is not None and other[0].name != "calc":
                return None   # "теперь переведи это", "давай включим музыку": another command, not a change
            return Intent(self.name, "revise", {"change": text}, text)
        return None


def create() -> Skill:
    return SolveSkill()
