"""Read-only access to user-allowed folders: resolve spoken folder names, list, count, size, find.

Nothing here writes, moves or deletes files. Every path is checked against [files] allowed_dirs
(plus the music and notes folders) before it is touched.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import winreg
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from rapidfuzz import fuzz, process

from assistant.config import Settings

log = logging.getLogger("files")

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


_SUBDIRS: dict[str, tuple[float, dict[str, Path]]] = {}


def _subfolders(cfg: Settings) -> dict[str, Path]:
    """Allowed roots and their subfolders (depth <= 2) by lowercase name; read at most once a minute."""
    key = "|".join(str(r) for r in allowed_roots(cfg))
    cached = _SUBDIRS.get(key)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
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
    _SUBDIRS[key] = (time.monotonic(), names)
    return names


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
    names = _subfolders(cfg)
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


# ------------------------------------------------------------------ Windows Search index and a walk cache
_index_lock = threading.Lock()
_indexed: dict[Path, bool] = {}
_TREE: dict[Path, tuple[float, float, list[Path]]] = {}   # root -> (built at, root mtime, files)
TREE_TTL = 120.0


def _query_index(sql: str) -> list[Path] | None:
    """Rows of a Windows Search query as real paths (System.ItemUrl); None if the index is unavailable."""
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        conn = win32com.client.Dispatch("ADODB.Connection")
        conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
        rs, _ = conn.Execute(sql)
        out: list[Path] = []
        while not rs.EOF:
            url = rs.Fields.Item("System.ItemUrl").Value or ""
            if url.startswith("file:"):
                out.append(Path(unquote(url[5:].lstrip("/") if url[5:7] != "//" else url[5:])))
            rs.MoveNext()
        conn.Close()
        return out
    except Exception as exc:
        log.debug("Индекс Windows недоступен: %s", exc)
        return None


def _scope(root: Path) -> str:
    return "SCOPE='file:" + str(root).replace("\\", "/").replace("'", "''") + "'"


def is_indexed(root: Path) -> bool:
    """Windows indexes the user folders by default; others (D:\\Проекты) are walked instead."""
    with _index_lock:
        if root not in _indexed:
            rows = _query_index(f"SELECT TOP 1 System.ItemUrl FROM SystemIndex WHERE {_scope(root)}")
            _indexed[root] = bool(rows)
        return _indexed[root]


def tree(root: Path) -> list[Path]:
    """Files under root from a short-lived cache (re-walked after 2 min or when the folder itself changes)."""
    try:
        mtime = root.stat().st_mtime
    except OSError:
        return []
    cached = _TREE.get(root)
    if cached and time.monotonic() - cached[0] < TREE_TTL and cached[1] == mtime:
        return cached[2]
    files = list(walk(root))
    _TREE[root] = (time.monotonic(), mtime, files)
    return files


def count(root: Path, exts: set[str] | None) -> int:
    # Not through the index: it hands rows over one by one (16 s for 40k files vs 1.7 s for a walk).
    return sum(1 for p in tree(root) if exts is None or p.suffix.lower() in exts)


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


def _candidates(roots: list[Path], query: str) -> list[Path]:
    """Files whose name may fit: one Windows index query for the indexed folders, the cached walk for the rest."""
    words = [w for w in re.findall(r"\w+", query) if len(w) >= 3][:4]
    indexed = [r for r in roots if words and is_indexed(r)]
    out: list[Path] = []
    if indexed:
        scopes = " OR ".join(_scope(r) for r in indexed)
        like = " OR ".join("System.FileName LIKE '%" + w.replace("'", "''") + "%'" for w in words)
        rows = _query_index(f"SELECT TOP 500 System.ItemUrl FROM SystemIndex WHERE ({scopes}) AND ({like})")
        if rows is None:
            indexed = []
        else:
            out += rows
    for root in roots:
        if root not in indexed:
            out += tree(root)
    return out


def _name_score(query: str, stem: str) -> float:
    if query in stem:
        return 100
    if len(query) < 4:
        return 0
    # partial_ratio of a long query against a tiny name ("vision" vs "n") would be 100.
    return fuzz.partial_ratio(query, stem) if len(stem) >= len(query) else fuzz.ratio(query, stem)


def find(cfg: Settings, name: str, limit: int = 5) -> list[Found]:
    """Fuzzy search by file name in all allowed folders."""
    query = name.lower().strip()
    scored: list[tuple[float, Path]] = []
    for p in dict.fromkeys(_candidates(allowed_roots(cfg), query)):
        score = _name_score(query, p.stem.lower())
        if score >= 85 and is_allowed(cfg, p):
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
