"""Dictation into the active window.

Recording starts after a short rising cue. Text goes in phrase by phrase while you speak (a pause between
sentences sends the phrase), not all at once at the end. Hotkeys are handled by app.py:
- Tap Ctrl+Alt+D: hands-free, each phrase is typed as soon as it is said; tap again or 30 s of silence stops.
- Hold Ctrl+Alt+D and speak: phrases are recognized as you go and typed when the keys are released
  (Ctrl+V cannot be pressed while Ctrl+Alt are held).
- "Джарвис, диктовка" -> "Диктуйте" -> cue -> speak; 3 s of silence ends it.
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
