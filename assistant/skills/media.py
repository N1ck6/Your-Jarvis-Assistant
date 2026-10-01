"""Volume and playback: louder/quieter, mute, pause, next/previous track (Windows media keys),
the volume of one app ("сделай дискорд тише", Windows mixer) and of Jarvis's own voice ("говори тише")."""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from assistant import winutil
from assistant.config import save_override
from assistant.skills.apps import CYR2LAT
from assistant.skills.base import Intent, Reply, Skill, Tool, run_blocking

_MUCH = re.compile(r"\b(намного|гораздо|сильно|значительно|еще|очень|максимально|по полной)\b")
_LITTLE = re.compile(r"\b(чуть|чуть-чуть|немного|немножко|слегка|капельку|самую малость)\b")
_SOUND = r"(звук|громкость|музыку|звучание)"
_QUIETER = r"(тише|потише|тихо|приглуши\w*|убавь|уменьши)"
_LOUDER = r"(громче|погромче|прибавь|увеличь)"

# Jarvis's own voice: "говори тише", "ты слишком громко говоришь", "сделай свой голос громче".
_SPEECH_QUIETER = re.compile(r"^(говори|разговаривай|отвечай)\s+(?:\w+\s+)?(тише|потише)$|^(тише|потише) говори$|"
                             r"\b(свой|твой) голос (тише|потише)$|^(сделай|убавь) (свой|твой) голос( тише)?$|"
                             r"^ты (говоришь |отвечаешь )?(слишком |очень )?громко( говоришь| отвечаешь)?$|^не (кричи|ори)$")
_SPEECH_LOUDER = re.compile(r"^(говори|разговаривай|отвечай)\s+(?:\w+\s+)?(громче|погромче)$|^(громче|погромче) говори$|"
                            r"\b(свой|твой) голос (громче|погромче)$|^(прибавь|увеличь) (свой|твой) голос$|"
                            r"^ты (говоришь |отвечаешь )?(слишком |очень )?тихо( говоришь| отвечаешь)?$|^тебя (плохо |не )?слышно$")
# One app in the Windows mixer: "сделай дискорд тише", "браузер погромче", "выключи звук в стиме".
_APP_LEVEL = re.compile(rf"^(?:сделай\s+|сделать\s+)?(?P<app>[а-яёa-z][\w .-]*?)\s+(?:\w+\s+)?(?P<dir>{_QUIETER}|{_LOUDER})$|"
                        rf"^(?P<verb>убавь|прибавь|уменьши|увеличь|понизь|подними)\s+(?:звук|громкость)\s+(?:в|у)\s+(?P<app2>.+)$")
_APP_MUTE = re.compile(r"^(?P<verb>выключи|отключи|убери|заглуши|верни|включи)\s+звук\s+(?:в|у)\s+(?P<app>.+)$|"
                       r"^(?P<verb2>заглуши|замьють)\s+(?P<app2>.+)$")
_NOT_APP = {"звук", "громкость", "музыку", "музыка", "звучание", "сделай", "немного", "чуть", "еще", "очень", "побольше",
            "поменьше", "вдвое", "сильно", "намного", "голос", "все", "всё", "радио", "телевизор"}

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
_REVERSE = {"vol_up": "vol_down", "vol_down": "vol_up", "mute": "unmute", "unmute": "mute", "next": "prev",
            "prev": "next", "pause": "resume", "resume": "pause"}


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
        'сделай <приложение> тише',
        'выключи звук в <приложение>',
        'говори тише (голос Джарвиса)',
    ]

    def match(self, text: str) -> Intent | None:
        steps = 12 if _MUCH.search(text) else 2 if _LITTLE.search(text) else 5
        if _SPEECH_QUIETER.search(text):
            return Intent(self.name, "speech_volume", {"delta": -0.15}, text)
        if _SPEECH_LOUDER.search(text):
            return Intent(self.name, "speech_volume", {"delta": 0.15}, text)
        if m := _APP_MUTE.match(text):
            app = (m.group("app") or m.group("app2") or "").strip()
            verb = m.group("verb") or m.group("verb2") or ""
            if app and app.split()[0] not in _NOT_APP:
                return Intent(self.name, "app_mute", {"app": app, "mute": verb not in ("верни", "включи")}, text)
        if m := _APP_LEVEL.match(text):
            app = (m.group("app") or m.group("app2") or "").strip()
            louder = bool(re.match(_LOUDER, m.group("dir") or "")) or (m.group("verb") or "") in ("прибавь", "увеличь", "подними")
            if app in ("музыку", "музыка", "музыки") and self.app.music.active:
                return Intent(self.name, "music_volume", {"delta": 0.1 if louder else -0.1}, text)
            if app and app.split()[0] not in _NOT_APP and len(app) >= 3:
                return Intent(self.name, "app_volume", {"app": app, "delta": 0.25 if louder else -0.25,
                                                        "steps": steps}, text)
        for action, pattern in _PATTERNS:
            if pattern.search(text):
                return Intent(self.name, action, {"steps": steps}, text)
        return None

    def tools(self) -> list[Tool]:
        return [Tool("media_control", "Громкость и воспроизведение: громче, тише, выключить/включить звук, пауза, "
                     "продолжить, следующий или предыдущий трек.",
                     {"type": "object", "properties": {"action": {"type": "string", "enum": [
                         "vol_up", "vol_down", "mute", "unmute", "pause", "resume", "next", "prev"]}},
                      "required": ["action"]}, "tool")]

    # ------------------------------------------------------------------ mixer apps
    def _app_exe(self, spoken: str) -> str:
        """'дискорд' -> 'discord.exe' among the apps that play sound right now."""
        apps = winutil.audio_apps()
        if not apps:
            return ""
        translit = self.app.cfg.apps.translit
        variants = {spoken, translit.get(spoken, ""), spoken.translate(CYR2LAT)}
        variants |= {v for k, v in translit.items() if fuzz.ratio(spoken, k) >= 85}
        best = (0.0, "")
        for exe in apps:
            name = exe.removesuffix(".exe")
            friendly = winutil.Window(0, "", 0, exe).friendly.lower()
            for v in filter(None, variants):
                score = max(fuzz.ratio(v, name), fuzz.ratio(v, friendly), fuzz.partial_ratio(v, name) if len(v) >= 4 else 0)
                if score > best[0]:
                    best = (score, exe)
        return best[1] if best[0] >= 80 else ""

    async def _app_level(self, spoken: str, delta: float, steps: int) -> Reply:
        exe = await run_blocking(self._app_exe, spoken)
        if not exe:
            # Not an app that plays sound now: "сделай звук немного тише" is the system volume.
            return await self.handle(Intent(self.name, "vol_up" if delta > 0 else "vol_down", {"steps": steps}))
        level = await run_blocking(winutil.app_volume, exe, delta)
        name = winutil.Window(0, "", 0, exe).friendly

        async def undo() -> Reply:
            await run_blocking(winutil.app_volume, exe, -delta)
            return Reply(f"Вернул громкость {name}.")

        return Reply(tool_result=f"громкость {name}: {round((level or 0) * 100)}%", listen_after=False, undo=undo)

    async def _app_mute(self, spoken: str, mute: bool) -> Reply:
        exe = await run_blocking(self._app_exe, spoken)
        if not exe:
            return Reply(f"Не слышу звука от «{spoken}».")
        await run_blocking(lambda: winutil.app_volume(exe, mute=mute))

        async def undo() -> Reply:
            await run_blocking(lambda: winutil.app_volume(exe, mute=not mute))
            return Reply("Вернул звук." if mute else "Снова без звука.")

        return Reply(tool_result="готово", listen_after=False, undo=undo)

    # ------------------------------------------------------------------ actions
    async def handle(self, intent: Intent) -> Reply:
        action = intent.slots.get("action") if intent.action == "tool" else intent.action
        steps = int(intent.slots.get("steps", 5))
        music = self.app.music
        match action:
            case "speech_volume":
                return self._speech_volume(float(intent.slots["delta"]))
            case "music_volume":
                before = music.volume
                music.volume = round(min(1.0, max(0.05, music.volume + float(intent.slots["delta"]))), 2)
                save_override("music.volume", music.volume)

                async def undo_music() -> Reply:
                    music.volume = before
                    save_override("music.volume", before)
                    return Reply("Вернул громкость музыки.")

                return Reply(listen_after=False, undo=undo_music)
            case "app_volume":
                return await self._app_level(str(intent.slots["app"]), float(intent.slots["delta"]), steps)
            case "app_mute":
                return await self._app_mute(str(intent.slots["app"]), bool(intent.slots.get("mute", True)))
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
                return Reply(f"Громкость {pct}%" + (", звук выключен." if muted else "."), listen_after=False)
        # Volume and playback are their own feedback: no words.
        reverse = _REVERSE.get(str(action))

        async def undo() -> Reply:
            await self.handle(Intent(self.name, reverse or "", {"steps": steps}))
            return Reply("Вернул как было.")

        return Reply(tool_result="готово", listen_after=False, undo=undo if reverse else None)

    def _speech_volume(self, delta: float) -> Reply:
        player = self.app.player
        before = player.volume
        player.volume = round(min(1.5, max(0.2, player.volume + delta)), 2)
        save_override("tts.volume", player.volume)
        self.app.cfg.tts.volume = player.volume

        async def undo() -> Reply:
            player.volume = before
            save_override("tts.volume", before)
            return Reply("Вернул прежнюю громкость голоса.")

        if player.volume == before:
            return Reply("Это предел, сэр." if delta > 0 else "Тише уже некуда.")
        return Reply("Так лучше?" if delta < 0 else "Так слышно?", undo=undo)


def create() -> Skill:
    return MediaSkill()
