"""Solve a task: from the phrase itself, from the selected text or from a region of the screen.

The task goes to the cloud model (images: the vision chain), the answer comes back in two parts and is shown in two
pinned windows: a short solution and the answer alone. Both are plain text: no markdown, LaTeX or wrapping brackets.

"реши задачу с экрана" asks for a region; "реши эту задачу" right after "что на экране" reuses that region;
"реши выделенное" takes the selection; "реши задачу: два поезда вышли..." solves what was said.
Settings: [config] of skill.toml; the prompt: prompts/solve.md; checked phrases: tests/phrases.toml.
"""
from __future__ import annotations

import logging
import re

from assistant import mathtext
from assistant.llm.hub import FAIL_TEXT
from assistant.llm.providers import Msg
from assistant.skills.base import Intent, Reply, Skill

log = logging.getLogger("solve")

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
_HEAD = re.compile(r"^\W*(решение|ответ|answer|solution)\W*?:?\s*", re.I)
_MD_LINE = re.compile(r"^\s*(?:#{1,6}\s*|>\s*|[-*•+]\s+|```\w*\s*$)")
_EMPH = re.compile(r"(\*\*|__|~~|`)")
_STAR = re.compile(r"(?<=[\w)])\s*\*\s*(?=[\w(])")
_SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
_WRAPPERS = [("(", ")"), ("[", "]"), ("{", "}"), ("«", "»"), ('"', '"'), ("'", "'"), ("“", "”")]


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

    async def handle(self, intent: Intent) -> Reply:
        source = intent.action
        task, images = "", []
        if source == "said":
            task = intent.raw or intent.text
        if source == "auto":
            recent = self.ctx.recent("screen.region", self.ctx.config.region_ttl_sec)
            if recent is not None:
                images = [recent]
            else:
                source = "selection_or_screen"
        if source in ("selection", "selection_or_screen"):
            task = (await self.ctx.selection())[:6000]
            if not task and source == "selection":
                return Reply("Сначала выделите задачу, а потом скажите «реши».")
        if not task and not images:
            png = await self.ctx.select_region()
            if not png:
                return Reply(listen_after=False)
            images = [png]
        log.info("Задача: %s", "картинка" if images else f"текст, {len(task)} символов")
        return Reply(stream=self._solve(task, images))

    async def _solve(self, task: str, images: list[bytes]):
        yield "Решаю. "
        cfg = self.ctx.config
        prompt = self.ctx.prompt("solve").replace("{where}", " с изображения" if images else "")
        messages = [Msg("user", prompt + (f"\n\nЗадача:\n{task}" if task else ""), images=images)]
        chain = [p for p in (cfg.image_chain if images else cfg.chain) if p != "local"]
        if cfg.local_fallback:
            chain.append("local")
        raw = await self.ctx.ask(messages, chain=chain, max_tokens=3000, models={"gemini": cfg.gemini_models})
        if not raw or raw == FAIL_TEXT:
            yield ("Не получилось решить: ни облако, ни локальная модель не ответили." if cfg.local_fallback else
                   "Не получилось решить: облако сейчас не отвечает, а локальной модели такие задачи я не доверяю.")
            return
        local = self.ctx.last_provider == "local"
        if "НЕТ ЗАДАЧИ" in raw.upper() and len(raw) < 40:
            yield "Задачи здесь не вижу."
            return
        solution, answer = split_solution(raw)
        log.info("Решено (%s): ответ %d символов", self.ctx.last_provider, len(answer))
        if solution:
            self.ctx.card("Решение", solution)
        if answer:
            self.ctx.card("Ответ · локально, может быть неточно" if local else "Ответ", answer)
        caution = "Облако недоступно, решил локально — лучше проверьте. " if local else ""
        if answer and len(answer) <= 120:
            yield caution + "Ответ: " + spoken(answer)
        else:
            yield caution + "Решение и ответ на экране."


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


def create() -> Skill:
    return SolveSkill()
