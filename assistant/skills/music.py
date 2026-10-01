"""Music from a local folder (default: Desktop/music), played by Jarvis's built-in player.

"включи <трек>", "включи музыку", "что играет", "сколько треков в папке музыка", "выключи музыку".
Pause / next / previous are handled by the media module and go to this player while it is active.
"""
from __future__ import annotations

import logging
import random
import re
from pathlib import Path

from rapidfuzz import fuzz, process

from assistant.nlu import normalize_command, plural
from assistant.skills.base import Intent, Reply, Skill, Tool

log = logging.getLogger("music")

AUDIO_EXT = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus", ".wma"}
_PLAY = re.compile(
    r"^(?:включи|включить|вруби|врубай|поставь|поставить|запусти|играй|сыграй|проиграй|воспроизведи|"
    r"хочу послушать|давай послушаем|послушаем|поставь мне|включи мне)\s+"
    r"(?P<kind>музыку|музычку|песню|песенку|трек|композицию|мелодию|что-нибудь|что нибудь|плейлист)?\s*(?P<name>.*)$"
)
_SHUFFLE_WORDS = {"", "любую", "любой", "что-нибудь", "что нибудь", "рандом", "случайную", "случайный", "все", "всю",
                  "подряд", "вперемешку", "из папки", "мою", "мои", "из моей папки"}
_NOW = re.compile(r"^(что|какая|какой|чья) (сейчас )?(играет|звучит|за песня|песня играет|трек играет|за трек|это песня)|^как называется (эта )?(песня|трек)")
_COUNT = re.compile(r"^сколько (у меня )?(песен|треков|композиций|музыки|записей|mp3)( у меня)?( в| во)?( папке| моей папке)?( с)?( музык\w*)?$|"
                    r"^сколько (у меня )?(файлов )?(в|во) (папке )?(с )?музык\w*$")
_STOP = re.compile(r"^(выключи|останови|отключи|вырубай|выруби|убери|стоп) (музыку|музон|плеер|песню|трек)$|^стоп музыка$|^хватит музыки$")


class MusicSkill(Skill):
    name = "music"
    title = "Музыка из папки"
    examples = [
        'включи <название трека>',
        'включи музыку',
        'что играет',
        'сколько треков в папке музыка',
        'выключи музыку',
    ]

    def __init__(self) -> None:
        self._tracks: dict[str, Path] = {}
        self._stamp: float = -1

    def _folder(self) -> Path:
        return self.app.cfg.resolve(self.app.cfg.music.dir)

    def tracks(self) -> dict[str, Path]:
        folder = self._folder()
        try:
            stamp = folder.stat().st_mtime
        except OSError:
            return {}
        if stamp != self._stamp:
            self._tracks = {normalize_command(p.stem): p for p in sorted(folder.rglob("*"))
                            if p.suffix.lower() in AUDIO_EXT}
            self._stamp = stamp
        return self._tracks

    def find(self, name: str) -> Path | None:
        tracks = self.tracks()
        if not tracks or not name:
            return None
        hit = process.extractOne(normalize_command(name), tracks.keys(), scorer=fuzz.WRatio, score_cutoff=80)
        return tracks[hit[0]] if hit else None

    def match(self, text: str) -> Intent | None:
        if _NOW.search(text):
            return Intent(self.name, "now", text=text)
        if _COUNT.match(text):
            return Intent(self.name, "count", text=text)
        if _STOP.match(text):
            return Intent(self.name, "stop", text=text)
        m = _PLAY.match(text)
        if not m:
            return None
        kind, name = m.group("kind") or "", m.group("name").strip()
        if kind and name in _SHUFFLE_WORDS:
            return Intent(self.name, "shuffle", {}, text)
        if self.find(name):
            return Intent(self.name, "play", {"name": name}, text)
        if kind in ("песню", "песенку", "трек", "композицию", "мелодию"):
            return Intent(self.name, "play", {"name": name}, text)  # explicit request: report "not found"
        return None  # "включи телеграм" -> apps

    def tools(self) -> list[Tool]:
        return [Tool("play_music", "Включить трек из папки с музыкой пользователя (пусто — всё вперемешку).",
                     {"type": "object", "properties": {"name": {"type": "string"}}}, "play"),
                Tool("music_info", "Сколько треков в папке с музыкой и что сейчас играет.",
                     {"type": "object", "properties": {}}, "count", direct=False)]

    async def handle(self, intent: Intent) -> Reply:
        player = self.app.music
        if intent.action == "now":
            if not player.active or player.current is None:
                return Reply("Сейчас ничего не играет.")
            return Reply(f"Играет {player.current.stem}.", listen_after=False)
        if intent.action == "stop":
            if player.active:
                player.stop()
            else:  # music in another app (browser, Spotify): pause it
                from assistant import winutil

                winutil.press(winutil.MEDIA_KEYS["play_pause"])
            return Reply(listen_after=False)
        tracks = list(self.tracks().values())
        if intent.action == "count":
            n = len(tracks)
            now = f" Сейчас играет {player.current.stem}." if player.active and player.current else ""
            return Reply(f"В папке с музыкой {n} {plural(n, 'трек', 'трека', 'треков')}.{now}",
                         tool_result=f"{n} треков")
        if not tracks:
            return Reply(f"Папка с музыкой пуста или не найдена: {self._folder()}.")
        name = str(intent.slots.get("name") or "").strip()
        if intent.action == "shuffle" or not name:
            order = random.sample(tracks, len(tracks))
            title = "музыку"
        else:
            first = self.find(name)
            if first is None:
                return Reply(f"Не нашёл трек «{name}» в папке с музыкой.")
            order = [first, *[t for t in tracks if t != first]]
            title = first.stem
        player.play(order)
        log.info("Музыка: %s (%d в очереди)", title, len(order))
        return Reply(f"Включаю {title}.", reaction="ok", tool_result=f"играет {title}", listen_after=False)


def create() -> Skill:
    return MusicSkill()
