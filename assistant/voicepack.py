"""Recorded Jarvis reactions from Priler's project (github.com/Priler/jarvis, CC BY-NC-SA 4.0).

A pack is a folder models/voices/<id>/ with voice.toml and ru/*.wav|mp3. Reactions:
greet, greet_morning/day/evening/night, reply (bare wake word), ok (action done),
not_found, thanks, goodbye, joke, stupid, game_mode.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import miniaudio
import numpy as np

from assistant.audio.player import AudioClip
from assistant.downloads import fetch
from assistant.paths import MODELS_DIR

log = logging.getLogger("voicepack")

PACKS_DIR = MODELS_DIR / "voices"
REPO_API = "https://api.github.com/repos/Priler/jarvis/contents/resources/sound/voices"
PACK_IDS = ["jarvis-remaster", "jarvis-howdy", "jarvis-og"]
PACK_TITLES = {
    "jarvis-remaster": "Jarvis Remaster — чистая запись, приветствия по времени суток",
    "jarvis-howdy": "Jarvis Howdy — голос из роликов Хауди Хо",
    "jarvis-og": "Jarvis OG — оригинальная озвучка из фильма",
}
# Files present in the packs but not listed in voice.toml.
_EXTRA = {"joke": "joke", "stupid": "stupid", "game_mode": "game_mode"}


@dataclass
class VoicePack:
    id: str
    title: str
    files: dict[str, list[Path]] = field(default_factory=dict)
    _cache: dict[Path, AudioClip] = field(default_factory=dict)
    _last: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def load(cls, pack_id: str) -> "VoicePack | None":
        folder = PACKS_DIR / pack_id
        manifest = folder / "voice.toml"
        audio_dir = folder / "ru"
        if not manifest.exists() or not audio_dir.exists():
            return None
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        reactions = data.get("reactions", {}).get("ru", {})
        by_stem = {p.stem: p for p in audio_dir.iterdir() if p.suffix.lower() in (".wav", ".mp3")}
        files: dict[str, list[Path]] = {}
        for reaction, stems in reactions.items():
            paths = [by_stem[s] for s in stems if s in by_stem]
            if paths:
                files[reaction] = paths
        for reaction, prefix in _EXTRA.items():
            paths = sorted(p for s, p in by_stem.items() if s == prefix or (s.startswith(prefix) and s[len(prefix):].isdigit()))
            if paths:
                files[reaction] = paths
        return cls(pack_id, PACK_TITLES.get(pack_id, data.get("voice", {}).get("name", pack_id)), files)

    def has(self, reaction: str) -> bool:
        return bool(self.files.get(reaction))

    def clip(self, path: Path) -> AudioClip:
        if path not in self._cache:
            d = miniaudio.decode_file(str(path), output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1)
            self._cache[path] = AudioClip(np.frombuffer(d.samples, dtype=np.float32).copy(), d.sample_rate)
        return self._cache[path]

    def pick(self, reaction: str) -> AudioClip | None:
        """Random clip for the reaction, never the same one twice in a row."""
        options = self.files.get(reaction) or []
        if not options:
            return None
        choices = [p for p in options if p != self._last.get(reaction)] or options
        path = random.choice(choices)
        self._last[reaction] = path
        return self.clip(path)

    def greeting(self) -> AudioClip | None:
        h = dt.datetime.now().hour
        part = "morning" if 5 <= h < 12 else "day" if 12 <= h < 18 else "evening" if 18 <= h < 23 else "night"
        return self.pick(f"greet_{part}") or self.pick("greet")


# Clean clips (with exact transcripts) that make a ~7 s reference for voice cloning. Shorter = faster synthesis.
CLONE_REFS: dict[str, list[tuple[str, str]]] = {
    "jarvis-og": [("reply1", "Да, сэр."), ("ok3", "Запрос выполнен, сэр."),
                  ("not_found", "Чего вы пытаетесь добиться, сэр?"), ("thanks", "Всегда к вашим услугам, сэр.")],
    "jarvis-remaster": [("greet_day", "Добрый день, сэр. Чем я могу вам сегодня помочь?"),
                        ("stupid", "Очень тонкое замечание, сэр."), ("thanks", "Всегда к вашим услугам, сэр.")],
    "jarvis-howdy": [("run", "Добрый день, сэр."), ("ready", "Мы подключены и готовы."),
                     ("not_found", "Чего вы пытаетесь добиться, сэр?"), ("thanks", "Всегда к вашим услугам, сэр.")],
}


def clone_reference(pack_id: str, sr: int = 24000) -> tuple[np.ndarray, str]:
    """Concatenated reference audio (float32 mono at `sr`) and its transcript."""
    folder = PACKS_DIR / pack_id / "ru"
    parts: list[np.ndarray] = []
    texts: list[str] = []
    gap = np.zeros(int(sr * 0.3), dtype=np.float32)
    for stem, text in CLONE_REFS[pack_id]:
        path = next(folder.glob(stem + ".*"), None)
        if path is None:
            continue
        d = miniaudio.decode_file(str(path), output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=sr)
        parts += [np.frombuffer(d.samples, dtype=np.float32), gap]
        texts.append(text)
    if not parts:
        raise FileNotFoundError(f"Нет реплик пакета {pack_id} для клонирования")
    return np.concatenate(parts), " ".join(texts)


def load_pack(pack_id: str) -> VoicePack | None:
    if not pack_id or pack_id == "none":
        return None
    pack = VoicePack.load(pack_id)
    if pack is None:
        log.warning("Голосовой пакет %s не найден в %s (python -m assistant.setup_models)", pack_id, PACKS_DIR)
    return pack


def available_packs() -> list[VoicePack]:
    return [p for p in (VoicePack.load(i) for i in PACK_IDS) if p is not None]


def download_packs() -> None:
    """Downloads Russian clips of all packs straight from Priler's repository."""
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for pack_id in PACK_IDS:
            folder = PACKS_DIR / pack_id
            if (folder / "voice.toml").exists() and (folder / "ru").exists():
                continue
            fetch(f"https://raw.githubusercontent.com/Priler/jarvis/master/resources/sound/voices/{pack_id}/voice.toml",
                  folder / "voice.toml", what=f"{pack_id}/voice.toml")
            listing = client.get(f"{REPO_API}/{pack_id}/ru", params={"ref": "master"})
            listing.raise_for_status()
            for item in listing.json():
                if item.get("type") == "file":
                    fetch(item["download_url"], folder / "ru" / item["name"], what=f"{pack_id}/{item['name']}")
