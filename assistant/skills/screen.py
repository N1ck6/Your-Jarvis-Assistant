"""Help with what is on the screen: the user selects a region, the image goes to a vision model.

Our own selector (freeze-frame + rectangle) is used instead of the Snipping Tool: the picture never
touches the disk, the clipboard or the clipboard history. Hotkey: Ctrl+Alt+S.
"""
from __future__ import annotations

import asyncio
import logging
import re

from assistant.llm.providers import Msg
from assistant.skills.base import Intent, Reply, Skill

log = logging.getLogger("screen")

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
OCR_PROMPT = ("Перепиши весь текст с изображения дословно, сохраняя строки и порядок. Без пояснений, кавычек и markdown. "
              "Если текста нет, ответь одним словом: НЕТ.")

PROMPT = """Пользователь сам выделил эту область экрана и сказал: «{question}».
Ответь по-русски на его просьбу по изображению: что это, в чём суть, что делать дальше.
Ответ будет озвучен: 2–4 коротких предложения без markdown. Если на картинке текст на другом языке и просят перевести — переведи."""


class ScreenSkill(Skill):
    async def _ocr(self, png: bytes) -> Reply:
        """Text from the selected region into the clipboard (a screenshot, a video, a locked PDF)."""
        from assistant import winutil
        from assistant.core import Card, Deck
        from assistant.skills.base import run_blocking

        text = (await self.app.llm.complete(self.app.cfg.llm.vision_chain, [Msg("user", OCR_PROMPT, images=[png])],
                                            web=False, max_tokens=1500)).strip()
        if not text or text.upper().startswith("НЕТ") or text.startswith("Не получилось"):
            return Reply("Текста в этой области не нашёл.")
        await run_blocking(winutil.set_clipboard_text, text)
        self.app.ui.show_deck(Deck(title="Текст с экрана", cards=[Card("", text, "")], done=True, pinned=True))
        return Reply("Текст в буфере обмена.", reaction="ok", tool_result=text[:500], listen_after=False)

    name = "screen"
    title = "Помощь с экраном"
    examples = [
        'что на экране',
        'скопируй текст с экрана',
    ]

    def match(self, text: str) -> Intent | None:
        if _OCR.search(text):
            return Intent(self.name, "ocr", text=text)
        if _TRIGGER.search(text):
            return Intent(self.name, "help", text=text)
        return None

    async def handle(self, intent: Intent) -> Reply:
        fut = self.app.ui.select_region()
        try:
            png = await asyncio.wait_for(asyncio.wrap_future(fut), timeout=90)
        except asyncio.TimeoutError:
            png = None
        if not png:
            return Reply(listen_after=False)
        log.info("Область экрана: %d КБ", len(png) // 1024)
        if intent.action == "ocr":
            return await self._ocr(png)
        question = intent.raw or intent.text or "помоги с этим"
        messages = [Msg("user", PROMPT.format(question=question), images=[png])]
        return Reply(stream=self.app.llm.stream(self.app.cfg.llm.vision_chain, messages, web=False, max_tokens=500))


def create() -> Skill:
    return ScreenSkill()
