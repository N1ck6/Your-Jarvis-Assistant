"""Owner's voice: "запомни мой голос" (three phrases), "забудь мой голос", "ты узнаёшь мой голос?".

With a profile and [wake] owner_only, Jarvis ignores "Джарвис" and follow-ups said in other voices (TV, guests).
"""
from __future__ import annotations

import logging
import re

from assistant.config import save_override
from assistant.skills.base import Intent, Reply, Skill, run_blocking

log = logging.getLogger("voiceprint")

_ENROLL = re.compile(r"^(запомни|выучи|запиши|изучи|настрой|сохрани)\s+(мой|мои)\s+(голос|голоса)|^(узнавай|слушай) только (меня|мой голос)|"
                     r"^(реагируй|отвечай) только на (меня|мой голос)$|^голосовой профиль$")
_FORGET = re.compile(r"^(забудь|сотри|удали|сбрось)\s+(мой\s+)?(голос|голосовой профиль|профиль голоса)|"
                     r"^(слушай|реагируй на|отвечай) (всех|всем)$")
_CHECK = re.compile(r"^(ты )?(узнаешь|узнал|узнаёшь) (меня|мой голос)|^(проверь|проверка) (мой )?голос\w*$|^это я$")
PROMPTS = ["Скажите: «Джарвис, какая сегодня погода».", "Теперь: «Поставь таймер на десять минут».",
           "И последнее: «Открой браузер и включи музыку»."]


class VoiceprintSkill(Skill):
    name = "voiceprint"
    title = "Голос владельца"
    examples = [
        'запомни мой голос',
        'забудь мой голос',
        'ты узнаешь мой голос',
    ]

    def match(self, text: str) -> Intent | None:
        for action, pattern in (("enroll", _ENROLL), ("forget", _FORGET), ("check", _CHECK)):
            if pattern.search(text):
                return Intent(self.name, action, text=text)
        return None

    async def handle(self, intent: Intent) -> Reply:
        app = self.app
        vp = app.voiceprint
        if intent.action == "forget":
            vp.forget()
            app.cfg.wake.owner_only = False
            save_override("wake.owner_only", False)
            return Reply("Профиль голоса удалён, снова слушаю всех.", listen_after=False)
        if not app.mic:
            return Reply("Для этого нужен микрофон.")
        if intent.action == "check":
            if not vp.enrolled:
                return Reply("Я ещё не знаю ваш голос. Скажите «запомни мой голос».")
            await app.speaker.say("Скажите любую фразу.")
            audio = await app.record_utterance(8)
            sim = await run_blocking(vp.similarity, audio) if audio is not None else None
            if sim is None:
                return Reply("Не расслышал, фраза слишком короткая.")
            log.info("Сходство с профилем: %.2f", sim)
            ok = sim >= app.cfg.wake.owner_threshold
            return Reply(f"{'Да, это вы' if ok else 'Голос не похож на ваш'}, сходство {round(sim * 100)}%.")
        # enroll
        await app.speaker.say("Запомню ваш голос по трём фразам. Говорите после сигнала.")
        clips = []
        for prompt in PROMPTS:
            await app.speaker.say(prompt)
            audio = await app.record_utterance(10)
            if audio is None:
                return Reply("Не услышал фразу, попробуем потом.")
            clips.append(audio)
        used = await run_blocking(vp.enroll, clips)
        if used < 2:
            return Reply("Фразы получились слишком короткими, давайте ещё раз позже.")
        app.cfg.wake.owner_only = True
        save_override("wake.owner_only", True)
        return Reply("Готово. Теперь я реагирую только на ваш голос. Отключить: «забудь мой голос» или в настройках.",
                     listen_after=False)


def create() -> Skill:
    return VoiceprintSkill()


