"""Daily requests/tokens per provider (data/usage.json). Shown on the settings page, used for soft limits."""
from __future__ import annotations

import datetime as dt
import json
import threading

from assistant.paths import DATA_DIR

FILE = DATA_DIR / "usage.json"
KEEP_DAYS = 30


class Usage:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        try:
            self.data: dict[str, dict[str, dict[str, int]]] = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    @staticmethod
    def _today() -> str:
        return dt.date.today().isoformat()

    def record(self, provider: str, tokens_in: int = 0, tokens_out: int = 0) -> None:
        with self._lock:
            day = self.data.setdefault(self._today(), {})
            row = day.setdefault(provider, {"requests": 0, "in": 0, "out": 0})
            row["requests"] += 1
            row["in"] += int(tokens_in or 0)
            row["out"] += int(tokens_out or 0)
            for old in sorted(self.data)[:-KEEP_DAYS]:
                del self.data[old]
            FILE.parent.mkdir(parents=True, exist_ok=True)
            FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")

    def today(self) -> dict[str, dict[str, int]]:
        return dict(self.data.get(self._today(), {}))

    def requests_today(self, provider: str) -> int:
        return self.data.get(self._today(), {}).get(provider, {}).get("requests", 0)
