"""Dictation into the active window.

- Hold Ctrl+Alt+D and speak: text appears where the cursor is (handled by app.py hotkeys).
- "Джарвис, диктовка" -> "Диктуйте" -> speak -> pause 1.5 s -> text is pasted.
- "Джарвис, напечатай привет, буду через 10 минут" -> the text is typed right away.
"""
from __future__ import annotations

import re

from assistant import winutil
from assistant.skills.base import Intent, Reply, Skill, run_blocking

_START = re.compile(
    r"^(диктовка|режим диктовки|начни диктовку|включи диктовку|диктуй|давай диктовать|буду диктовать|"
    r"пиши под диктовку|записывай под диктовку|запиши под диктовку|запиши текст|пиши текст|набери текст|"
    r"напиши текст|надиктую|я продиктую|продиктую|записывай|печатай|пиши за мной|записывай за мной)$"
)
_INLINE = re.compile(r"^(напечатай|набери|впиши|вставь текст|напиши текстом|введи текст|набери текстом)\s+(?P<text>.+)$")
_INLINE_RAW = re.compile(r"^\W*(напечатай|набери|впиши|вставь текст|напиши текстом|введи текст|набери текстом)\W+", re.I)


class DictationSkill(Skill):
    name = "dictation"
    title = "Диктовка"
    examples = [
        'диктовка',
        'напечатай <текст>',
    ]

    def match(self, text: str) -> Intent | None:
        if _START.match(text):
            return Intent(self.name, "start", text=text)
        m = _INLINE.match(text)
        if m:
            return Intent(self.name, "type", {"text": m.group("text")}, text)
        return None

    async def handle(self, intent: Intent) -> Reply:
        if intent.action == "type":
            # Keep the original case and punctuation from the recognizer.
            text = _INLINE_RAW.sub("", intent.raw).strip() if intent.raw else intent.slots["text"]
            await run_blocking(winutil.paste_text, text)
            return Reply(listen_after=False)
        if not self.app.mic:
            return Reply("Диктовка работает только с микрофоном.")
        await self.app.speaker.say("Диктуйте.")
        self.app.start_dictation(until_pause=True, paste=winutil.paste_text)
        return Reply(spoken=True, listen_after=False)


def create() -> Skill:
    return DictationSkill()
