"""Voice settings: open the voice lab page, change speech rate."""
from __future__ import annotations

import re

from assistant.config import save_override
from assistant.skills.base import Intent, Reply, Skill

_LAB = re.compile(r"(выбор|выбрать|выбери|смени|поменяй|настрой\w*|сменить|поменять|другой|измени)\s+(свой\s+)?(голос|голоса|озвучк\w*|реплики)|"
                  r"(открой|покажи) (выбор|настройки) (голоса|озвучки)|^настройки голоса$|^(голос|голоса) джарвиса$")
_SETTINGS = re.compile(r"^(открой|покажи|зайди в|открыть)( мне)? (свои |твои |мои )?(настройки|параметры)"
                       r"( джарвиса| ассистента| приложения| программы)?$|^настройки( джарвиса)?$")
_FASTER = re.compile(r"говори (быстрее|побыстрее|живее)|(ускорь|ускорить) (речь|голос)|ты (говоришь|разговариваешь) (слишком )?медленно|^быстрее говори")
_SLOWER = re.compile(r"говори (медленнее|помедленнее|спокойнее)|(замедли|замедлить) (речь|голос)|ты (говоришь|тараторишь|разговариваешь) (слишком )?быстро|^не тараторь")


class VoiceSkill(Skill):
    name = "voice"
    title = "Настройки голоса"
    examples = [
        'смени голос',
        'открой настройки',
        'говори быстрее',
        'говори медленнее',
    ]

    def match(self, text: str) -> Intent | None:
        if _LAB.search(text):
            return Intent(self.name, "lab", text=text)
        if _SETTINGS.search(text):
            return Intent(self.name, "settings", text=text)
        if _FASTER.search(text):
            return Intent(self.name, "rate", {"delta": 0.1}, text)
        if _SLOWER.search(text):
            return Intent(self.name, "rate", {"delta": -0.1}, text)
        return None

    async def handle(self, intent: Intent) -> Reply:
        if intent.action == "lab":
            self.app.ui.open_settings(self.app.voicelab_url)
            return Reply("Открываю настройки голоса.", listen_after=False)
        if intent.action == "settings":
            self.app.ui.open_settings(self.app.voicelab_url)
            return Reply("Открываю настройки.", listen_after=False)
        tts = self.app.tts
        before = tts.rate
        tts.rate = round(min(1.5, max(0.7, tts.rate + float(intent.slots["delta"]))), 2)
        save_override("tts.rate", tts.rate)

        async def undo() -> Reply:
            tts.rate = before
            save_override("tts.rate", before)
            return Reply("Вернул прежнюю скорость.")

        return Reply("Хорошо, так лучше?", undo=undo)


def create() -> Skill:
    return VoiceSkill()
