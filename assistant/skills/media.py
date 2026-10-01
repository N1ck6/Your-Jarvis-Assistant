"""Volume and playback: louder/quieter, mute, pause, next/previous track (Windows media keys)."""
from __future__ import annotations

import re

from assistant import winutil
from assistant.skills.base import Intent, Reply, Skill, Tool, run_blocking

_MUCH = re.compile(r"\b(намного|гораздо|сильно|значительно|еще|очень|максимально|по полной)\b")
_LITTLE = re.compile(r"\b(чуть|чуть-чуть|немного|немножко|слегка|капельку|самую малость)\b")
_SOUND = r"(звук|громкость|музыку|звучание)"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("unmute", re.compile(r"^(включи|верни|вруби|врубай|включить|вернуть)\s+(обратно\s+)?звук\b|\bсними (с )?беззвучн|\bразмьють|\bзвук (включи|верни|обратно)")),
    ("mute", re.compile(r"^(выключи|отключи|убери|вырубай|выруби|заглуши|выключить|отключить|вырубить)\s+(весь\s+)?звук\b|^без звука$|\bбеззвучн\w* режим|^(мьют|замьють|заглуши)$|^звук (выключи|убери|отключи)")),
    ("vol_up", re.compile(r"\b(громче|погромче|прибавь|прибавить)\b|\b(увеличь|увеличить|добавь|добавить|повысь|подними|подними-ка) (звук|громкост\w*)|"
                          rf"^{_SOUND} (громче|погромче|выше|побольше|повыше)")),
    ("vol_down", re.compile(r"\b(тише|потише|убавь|убавить)\b|\b(уменьши|уменьшить|понизь|опусти|приглуши|снизь) (звук|громкост\w*)|^приглуши$|"
                            rf"^{_SOUND} (тише|потише|ниже|поменьше|пониже)")),
    ("next", re.compile(r"^(давай |включи )?следующ\w*( трек| песн\w*| композици\w*| видео)?$|\bследующ\w* (трек|песн\w*|композици\w*)|\b(скипни|скипай|пропусти|переключи)( эт\w*)? (трек|песн\w*|композици\w*)|\bдругую песню|\bдругой трек|^некст$|^дальше трек")),
    ("prev", re.compile(r"^(давай |включи )?предыдущ\w*( трек| песн\w*| композици\w*)?$|\bпредыдущ\w* (трек|песн\w*|композици\w*)|\b(верни|включи) (прошл|предыдущ)\w* (трек|песн\w*)|\bпрошл(ый|ую) (трек|песню)|^трек назад$")),
    ("pause", re.compile(r"^(пауза|на паузу|поставь на паузу|паузу)$|\b(поставь|поставить) (на )?паузу|\b(останови|приостанови|стопни|тормозни)\s+(музыку|видео|воспроизведение|трек|песню|плеер|ролик)")),
    ("resume", re.compile(r"^(продолжи|продолжай|играй|плей|play)( играть| воспроизведение| музыку| видео)?$|\bсними (с )?паузы|\bсними паузу|\bиграй дальше|\bвключи (музыку )?обратно|\bпродолжи (воспроизведение|музыку|видео)")),
    ("volume", re.compile(r"\b(какая|сколько|какой уровень) (сейчас )?громкост|^громкость$")),
]


class MediaSkill(Skill):
    name = "media"
    title = "Громкость и воспроизведение"
    examples = [
        'громче',
        'тише',
        'выключи звук',
        'включи звук',
        'пауза',
        'продолжи',
        'следующий трек',
        'предыдущий трек',
        'какая громкость',
    ]

    def match(self, text: str) -> Intent | None:
        for action, pattern in _PATTERNS:
            if pattern.search(text):
                steps = 5
                if _MUCH.search(text):
                    steps = 12
                elif _LITTLE.search(text):
                    steps = 2
                return Intent(self.name, action, {"steps": steps}, text)
        return None

    def tools(self) -> list[Tool]:
        return [Tool("media_control", "Громкость и воспроизведение: громче, тише, выключить/включить звук, пауза, "
                     "продолжить, следующий или предыдущий трек.",
                     {"type": "object", "properties": {"action": {"type": "string", "enum": [
                         "vol_up", "vol_down", "mute", "unmute", "pause", "resume", "next", "prev"]}},
                      "required": ["action"]}, "tool")]

    async def handle(self, intent: Intent) -> Reply:
        action = intent.slots.get("action") if intent.action == "tool" else intent.action
        steps = int(intent.slots.get("steps", 5))
        music = self.app.music
        match action:
            case "vol_up":
                await run_blocking(winutil.change_volume, steps)
            case "vol_down":
                await run_blocking(winutil.change_volume, -steps)
            case "mute":
                await run_blocking(winutil.set_mute, True)
            case "unmute":
                await run_blocking(winutil.set_mute, False)
            # Jarvis's own player gets the command while it is active; otherwise any app via media keys.
            case "pause" if music.active:
                music.pause()
            case "resume" if music.active:
                music.resume()
            case "next" if music.active:
                music.next()
            case "prev" if music.active:
                music.prev()
            case "pause" | "resume":
                await run_blocking(winutil.press, winutil.MEDIA_KEYS["play_pause"])
            case "next":
                await run_blocking(winutil.press, winutil.MEDIA_KEYS["next"])
            case "prev":
                await run_blocking(winutil.press, winutil.MEDIA_KEYS["prev"])
            case "volume":
                pct = await run_blocking(winutil.volume_percent)
                muted = await run_blocking(winutil.is_muted)
                if pct is None:
                    return Reply("Не могу узнать громкость.")
                return Reply(f"Громкость {pct} процентов" + (", звук выключен." if muted else "."), listen_after=False)
        # Volume and playback are their own feedback: no words.
        return Reply(tool_result="готово", listen_after=False)


def create() -> Skill:
    return MediaSkill()
