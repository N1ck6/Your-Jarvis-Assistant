"""Read-only access to user-allowed folders: resolve spoken folder names, list, count, size, find.

Nothing here writes, moves or deletes files. Every path is checked against [files] allowed_dirs
(plus the music and notes folders) before it is touched.
"""
from __future__ import annotations

import os
import time
import winreg
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

from assistant.config import Settings

_SHELL_KEYS = {
    "desktop": "Desktop", "documents": "Personal", "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "music": "My Music", "pictures": "My Pictures", "videos": "My Video",
}
# spoken (normalized) -> known folder
SPOKEN = {
    "рабочий стол": "desktop", "рабочем столе": "desktop", "рабочего стола": "desktop", "десктоп": "desktop",
    "документы": "documents", "документах": "documents", "документов": "documents", "мои документы": "documents",
    "загрузки": "downloads", "загрузках": "downloads", "загрузок": "downloads", "скачанные": "downloads",
    "закачки": "downloads", "downloads": "downloads",
    "музыка": "music", "музыке": "music", "музыки": "music", "музыкой": "music",
    "изображения": "pictures", "изображениях": "pictures", "картинки": "pictures", "картинках": "pictures",
    "фото": "pictures", "фотографии": "pictures", "фотографиях": "pictures", "pictures": "pictures",
    "видео": "videos", "видеозаписи": "videos", "videos": "videos",
}
KINDS = {
    "музык": {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus", ".wma"},
    "песен": {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus", ".wma"},
    "трек": {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus", ".wma"},
    "фото": {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp", ".raw", ".cr2", ".nef"},
    "картин": {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp"},
    "изображ": {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp", ".svg"},
    "видео": {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv"},
    "ролик": {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv"},
    "документ": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".odt", ".rtf", ".csv"},
    "pdf": {".pdf"}, "пдф": {".pdf"},
    "архив": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "программ": {".exe", ".msi"}, "установщик": {".exe", ".msi"},
}
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "appdata", "$recycle.bin"}


def known_folder(key: str) -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            value, _ = winreg.QueryValueEx(k, _SHELL_KEYS[key])
        return Path(os.path.expandvars(value))
    except OSError:
        return None


def _expand(cfg: Settings, raw: str) -> Path:
    p = raw.strip()
    home_map = {"~/desktop": "desktop", "~/documents": "documents", "~/downloads": "downloads",
                "~/music": "music", "~/pictures": "pictures", "~/videos": "videos"}
    key = home_map.get(p.lower().replace("\\", "/"))
    if key and (real := known_folder(key)) is not None:
        return real
    return cfg.resolve(p)


def allowed_roots(cfg: Settings) -> list[Path]:
    roots = [_expand(cfg, d) for d in cfg.files.allowed_dirs]
    roots += [cfg.resolve(cfg.music.dir), cfg.resolve(cfg.notes.dir)]
    out: list[Path] = []
    for r in roots:
        try:
            r = r.resolve()
        except OSError:
            continue
        if r.exists() and r not in out:
            out.append(r)
    return out


def is_allowed(cfg: Settings, path: Path) -> bool:
    try:
        path = path.resolve()
    except OSError:
        return False
    return any(path == r or r in path.parents for r in allowed_roots(cfg))


def resolve_dir(cfg: Settings, spoken: str) -> Path | None:
    """'загрузках' -> Downloads, 'папке проекты' -> an allowed subfolder named like that."""
    spoken = spoken.strip().removeprefix("папке ").removeprefix("папка ").removeprefix("папку ").strip()
    if not spoken:
        return None
    if spoken in SPOKEN:
        folder = known_folder(SPOKEN[spoken])
        if folder is not None and is_allowed(cfg, folder):
            return folder
    if spoken.startswith("музык"):
        return cfg.resolve(cfg.music.dir)
    if spoken.startswith("замет"):
        return cfg.resolve(cfg.notes.dir)
    # A subfolder (depth <= 2) of an allowed root, fuzzy by name.
    names: dict[str, Path] = {}
    for root in allowed_roots(cfg):
        names.setdefault(root.name.lower(), root)
        try:
            for child in root.iterdir():
                if child.is_dir() and child.name.lower() not in _SKIP_DIRS:
                    names.setdefault(child.name.lower(), child)
                    for grand in child.iterdir():
                        if grand.is_dir():
                            names.setdefault(grand.name.lower(), grand)
        except OSError:
            continue
    hit = process.extractOne(spoken, names.keys(), scorer=fuzz.WRatio, score_cutoff=85)
    return names[hit[0]] if hit else None


def kind_exts(spoken_kind: str) -> set[str] | None:
    for stem, exts in KINDS.items():
        if stem in spoken_kind:
            return exts
    return None  # all files


def walk(root: Path, max_files: int = 60000, max_sec: float = 4.0):
    """Files under root, skipping heavy/system folders, bounded in count and time."""
    deadline = time.monotonic() + max_sec
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            yield Path(dirpath) / f
            n += 1
            if n >= max_files or time.monotonic() > deadline:
                return


@dataclass
class Found:
    path: Path
    size: int
    mtime: float


def count(root: Path, exts: set[str] | None) -> int:
    return sum(1 for p in walk(root) if exts is None or p.suffix.lower() in exts)


def folder_size(root: Path) -> tuple[int, int]:
    total = files = 0
    for p in walk(root, max_sec=8.0):
        try:
            total += p.stat().st_size
            files += 1
        except OSError:
            pass
    return total, files


def recent(root: Path, limit: int = 5) -> list[Found]:
    items = []
    try:
        for p in root.iterdir():
            try:
                st = p.stat()
                items.append(Found(p, st.st_size, st.st_mtime))
            except OSError:
                pass
    except OSError:
        return []
    return sorted(items, key=lambda f: f.mtime, reverse=True)[:limit]


def find(cfg: Settings, name: str, limit: int = 5) -> list[Found]:
    """Fuzzy search by file name in all allowed folders."""
    query = name.lower().strip()
    scored: list[tuple[float, Path]] = []
    for root in allowed_roots(cfg):
        for p in walk(root, max_sec=3.0):
            stem = p.stem.lower()
            score = 100 if query in stem else fuzz.partial_ratio(query, stem) if len(query) >= 4 else 0
            if score >= 85:
                scored.append((score, p))
    scored.sort(key=lambda x: (-x[0], len(x[1].name)))
    out = []
    for _, p in scored[:limit]:
        try:
            st = p.stat()
            out.append(Found(p, st.st_size, st.st_mtime))
        except OSError:
            pass
    return out


_RU_NAMES = {"desktop": ("Рабочий стол", "на рабочем столе"), "documents": ("Документы", "в документах"),
             "downloads": ("Загрузки", "в загрузках"), "music": ("Музыка", "в папке с музыкой"),
             "pictures": ("Изображения", "в изображениях"), "videos": ("Видео", "в папке видео")}


def _known_key(path: Path) -> str | None:
    for key in _SHELL_KEYS:
        folder = known_folder(key)
        try:
            if folder is not None and folder.resolve() == path.resolve():
                return key
        except OSError:
            continue
    return None


def display_name(path: Path) -> str:
    """'Downloads' -> 'Загрузки' for known folders, otherwise the folder name."""
    key = _known_key(path)
    return _RU_NAMES[key][0] if key else path.name


def spoken_place(path: Path) -> str:
    """'в загрузках' / 'в папке Проекты'."""
    key = _known_key(path)
    return _RU_NAMES[key][1] if key else f"в папке {path.name}"


def human_size(n: int) -> str:
    for unit, div in (("ГБ", 2**30), ("МБ", 2**20), ("КБ", 2**10)):
        if n >= div:
            v = n / div
            return f"{v:.1f} {unit}".replace(".0 ", " ").replace(".", ",")
    return f"{n} байт"
