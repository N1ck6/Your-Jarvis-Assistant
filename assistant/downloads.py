"""Model downloads with retries (the VPN tunnel drops TLS handshakes now and then)."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("download")


def fetch(url: str, dest: Path, *, attempts: int = 5, what: str = "") -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            log.info("Скачиваю %s (%d/%d)", what or dest.name, i, attempts)
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
            os.replace(tmp, dest)
            return dest
        except (httpx.HTTPError, OSError) as exc:
            last = exc
            time.sleep(min(2 * i, 8))
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"Не удалось скачать {url}: {last}")
