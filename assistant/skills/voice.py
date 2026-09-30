"""Voice settings: open the voice lab page, change speech rate."""
from __future__ import annotations

import re

from assistant.config import save_override
from assistant.skills.base import Intent, Reply, Skill

_LAB = re.compile(r"(выбор|выбрать|выбери|смени|поменяй|настрой\w*|сменить|поменять|другой|измени)\s+(свой\s+)?(голос|голоса|озвучк\w*|реплики)|"
                  r"(открой|покажи) (выбор|настройки) (голоса|озвучки)|^настройки голоса$|^(голос|голоса) джарвиса$")
_FASTER = re.compile(r"говори (быстрее|побыстрее|живее)|(ускорь|ускорить) (речь|голос)|ты (говоришь|разговариваешь) (слишком )?медленно|^быстрее говори")
_SLOWER = re.compile(r"говори (медленнее|помедленнее|спокойнее)|(замедли|замедлить) (речь|голос)|ты (говоришь|тараторишь|разговариваешь) (слишком )?быстро|^не тараторь")


class VoiceSkill(Skill):
    name = "voice"
    title = "Настройки голоса"

    def match(self, text: str) -> Intent | None:
        if _LAB.search(text):
            return Intent(self.name, "lab", text=text)
        if _FASTER.search(text):
            return Intent(self.name, "rate", {"delta": 0.1}, text)
        if _SLOWER.search(text):
            return Intent(self.name, "rate", {"delta": -0.1}, text)
        return None

    async def handle(self, intent: Intent) -> Reply:
        if intent.action == "lab":
            self.app.ui.open_url(self.app.voicelab_url)
            return Reply("Открываю страницу выбора голоса.", listen_after=False)
        tts = self.app.tts
        tts.rate = round(min(1.5, max(0.7, tts.rate + float(intent.slots["delta"]))), 2)
        save_override("tts.rate", tts.rate)
        return Reply("Хорошо, так лучше?")


def create() -> Skill:
    return VoiceSkill()
