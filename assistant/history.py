"""Request history (Настройки → «Сохранять историю запросов»): data/history.jsonl, one line per request.

For the owner: what was asked, how Jarvis understood it and what he answered, to find and fix wrong answers. "не то"
marks the request it corrects as wrong. On start the last minutes are put back into the conversation, so a restart
(an update) does not make Jarvis forget what was just asked.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from assistant.paths import DATA_DIR

log = logging.getLogger("history")

FILE = DATA_DIR / "history.jsonl"
KEEP = 5000          # records kept in the file, oldest dropped
_lock = threading.Lock()
_last = 0.0


def _read() -> list[dict]:
    try:
        lines = FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue   # a line cut by a crash
    return out


def _write(items: list[dict]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
    tmp.replace(FILE)


def add(query: str, answer: str, route: str = "", sec: float = 0.0) -> str:
    """Appends a record; returns its id."""
    global _last
    with _lock:
        _last = max(time.time(), _last + 0.001)   # unique even for two requests in the same millisecond
        rid = f"{_last:.3f}"
    item = {"id": rid, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "query": query, "route": route,
            "answer": answer, "sec": round(sec, 2), "wrong": False, "note": ""}
    with _lock:
        try:
            FILE.parent.mkdir(parents=True, exist_ok=True)
            with FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            if FILE.stat().st_size > KEEP * 600:   # rarely: trim to the newest records
                _write(_read()[-KEEP:])
        except OSError as exc:
            log.warning("История не записана: %s", exc)
    return rid


def items(limit: int = 300) -> list[dict]:
    """The newest first."""
    with _lock:
        return _read()[-limit:][::-1]


def recent(within_sec: float) -> list[dict]:
    """Records of the last within_sec seconds, oldest first."""
    since = time.time() - within_sec
    with _lock:
        return [i for i in _read() if float(i.get("id", 0)) >= since]


def mark(rid: str, wrong: bool, note: str = "") -> bool:
    with _lock:
        all_ = _read()
        for i in all_:
            if i.get("id") == rid:
                i["wrong"], i["note"] = wrong, note
                _write(all_)
                return True
    return False


def mark_last_wrong(note: str = "") -> None:
    """"не то": the newest record that is not itself a correction."""
    with _lock:
        all_ = _read()
        if all_:
            all_[-1]["wrong"] = True
            if note and not all_[-1].get("note"):
                all_[-1]["note"] = note
            _write(all_)


def delete(rid: str) -> None:
    with _lock:
        _write([i for i in _read() if i.get("id") != rid])


def clear() -> None:
    with _lock:
        FILE.unlink(missing_ok=True)
