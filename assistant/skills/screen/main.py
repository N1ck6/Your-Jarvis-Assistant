"""Help with what is on the screen: the user selects a region, the image goes to a vision model.

Our own selector (freeze-frame + rectangle) is used instead of the Snipping Tool: the picture never
touches the disk, the clipboard or the clipboard history. Hotkey: Ctrl+Alt+S.
Prompts: prompts/help.md, prompts/ocr.md; what to do with the answer: commands.toml.
"""
from __future__ import annotations

import re

from assistant.skills.base import Intent, Skill

_SCREEN = r"(экран\w*|скрин\w*|окн[оеа]|мониторе?)"
_TRIGGER = re.compile(
    rf"(что (у меня |сейчас )?(на|тут на|здесь на|в) {_SCREEN}|посмотри на {_SCREEN}|глянь на {_SCREEN}|взгляни на {_SCREEN}|"
    rf"помоги с (этим|экраном|этим окном|этой ошибкой|этим текстом на экране)|помоги разобраться с (этим|экраном)|"
    rf"что (тут|здесь) (написано|изображено|нарисовано|происходит|за ошибка|показано)|что это за (окно|ошибка|программа|сообщение)|"
    rf"(объясни|переведи|прочитай|разбери|опиши|посмотри) (что |текст |то что |все |написанное |надпись )?(на|с|в) {_SCREEN}|"
    rf"(объясни|переведи|прочитай|разбери|опиши) (эт\w+ )?(ошибку|окно|картинку|график|схему|таблицу|формулу) на {_SCREEN}|"
    rf"^(сделай )?(скрин|скриншот)|^разбери скрин|^помощь с экраном|^что на экране$|^экран$)"
)

_OCR = re.compile(rf"^(скопируй|распознай|вытащи|извлеки|сними|перепиши|забери)\s+(весь\s+)?текст\s+(с|со|на|из)\s+{_SCREEN}|"
                  rf"^(текст с экрана|распознай текст|скопируй текст с картинки)$")

class ScreenSkill(Skill):
    """Recognizes the phrase; the rest is in commands.toml (prompt commands "help" and "ocr")."""

    def match(self, text: str) -> Intent | None:
        if _OCR.search(text):
            return Intent(self.name, "ocr", text=text)
        if _TRIGGER.search(text):
            return Intent(self.name, "help", text=text)
        return None


def create() -> Skill:
    return ScreenSkill()
