"""Music from a local folder (default: Desktop/music): "включи <трек>", "включи музыку".

Plays through the default Windows player with a playlist, so "следующий трек" works via media keys.
"""
from __future__ import annotations

import logging
import os
import random
import re
from pathlib import Path

from rapidfuzz import fuzz, process

from assistant.nlu import normalize_command
from assistant.paths import DATA_DIR
from assistant.skills.base import Intent, Reply, Skill, Tool, run_blocking

log = logging.getLogger("music")

AUDIO_EXT = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus", ".wma"}
_PLAY = re.compile(
    r"^(?:включи|включить|вруби|врубай|поставь|поставить|запусти|играй|сыграй|проиграй|воспроизведи|"
    r"хочу послушать|давай послушаем|послушаем|поставь мне|включи мне)\s+"
    r"(?P<kind>музыку|музычку|песню|песенку|трек|композицию|мелодию|что-нибудь|что нибудь|плейлист)?\s*(?P<name>.*)$"
)
_SHUFFLE_WORDS = {"", "любую", "любой", "что-нибудь", "что нибудь", "рандом", "случайную", "случайный", "все", "всю",
                  "подряд", "вперемешку", "из папки", "мою", "мои"}


class MusicSkill(Skill):
    name = "music"
    title = "Музыка из папки"

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
        m = _PLAY.match(text)
        if not m:
            return None
        kind, name = m.group("kind") or "", m.group("name").strip()
        if kind and name in _SHUFFLE_WORDS:
            return Intent(self.name, "shuffle", {}, text)
        track = self.find(name)
        if track:
            return Intent(self.name, "play", {"name": name}, text)
        if kind in ("песню", "песенку", "трек", "композицию", "мелодию"):
            return Intent(self.name, "play", {"name": name}, text)  # explicit request: report "not found"
        return None  # "включи телеграм" -> apps

    def tools(self) -> list[Tool]:
        return [Tool("play_music", "Включить трек из папки с музыкой пользователя (пусто — всё вперемешку).",
                     {"type": "object", "properties": {"name": {"type": "string"}}}, "play")]

    async def handle(self, intent: Intent) -> Reply:
        tracks = list(self.tracks().values())
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
            rest = [t for t in tracks if t != first]
            order = [first, *rest]
            title = first.stem
        await run_blocking(_play, order)
        log.info("Музыка: %s (%d в очереди)", title, len(order))
        return Reply(f"Включаю {title}.", reaction="ok", tool_result=f"играет {title}", listen_after=False)


def _play(order: list[Path]) -> None:
    playlist = DATA_DIR / "music_queue.m3u8"
    playlist.parent.mkdir(parents=True, exist_ok=True)
    playlist.write_text("#EXTM3U\n" + "\n".join(str(p) for p in order) + "\n", encoding="utf-8")
    try:
        os.startfile(playlist)  # type: ignore[attr-defined]
    except OSError:
        os.startfile(order[0])  # type: ignore[attr-defined]  # no player for playlists: first track only


def create() -> Skill:
    return MusicSkill()
