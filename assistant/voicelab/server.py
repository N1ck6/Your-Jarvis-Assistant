"""Voice lab: listen to Jarvis's synthesized voice and recorded reaction packs, pick a pack."""
from __future__ import annotations

import io
import logging
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from assistant.config import save_override
from assistant.tts.catalog import VOICES
from assistant.tts.manager import TtsManager
from assistant.voicepack import PACK_IDS, VoicePack

log = logging.getLogger("voicelab")
STATIC = Path(__file__).parent / "static"
REACTION_TITLES = {
    "greet": "Запуск", "greet_morning": "Утро", "greet_day": "День", "greet_evening": "Вечер", "greet_night": "Ночь",
    "reply": "На «Джарвис»", "ok": "Выполнено", "not_found": "Не понял", "thanks": "На «спасибо»",
    "goodbye": "Выход", "joke": "Шутки", "stupid": "На грубость", "game_mode": "Игровой режим",
}


class SynthRequest(BaseModel):
    text: str
    rate: float = 1.0


class PackRequest(BaseModel):
    pack: str
    rate: float = 1.0


def _wav_bytes(samples: np.ndarray, sr: int) -> bytes:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def create_app(tts: TtsManager, current_pack: Callable[[], str],
               on_pack: Callable[[str], None] | None = None) -> FastAPI:
    app = FastAPI(title="Jarvis Voice", docs_url=None, redoc_url=None)
    packs: dict[str, VoicePack] = {}

    def pack(pack_id: str) -> VoicePack:
        if pack_id not in packs:
            loaded = VoicePack.load(pack_id)
            if loaded is None:
                raise HTTPException(404, f"Пакет {pack_id} не скачан (python -m assistant.setup_models)")
            packs[pack_id] = loaded
        return packs[pack_id]

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/api/state")
    def state() -> dict:
        out = []
        for pid in PACK_IDS:
            p = VoicePack.load(pid)
            if p is None:
                continue
            out.append({"id": pid, "title": p.title, "reactions": [
                {"key": k, "title": REACTION_TITLES.get(k, k), "count": len(v)} for k, v in p.files.items()]})
        return {"voice": VOICES[0].to_dict(), "rate": tts.rate, "pack": current_pack(), "packs": out}

    @app.post("/api/synth")
    def synth(req: SynthRequest) -> Response:
        text = req.text.strip()[:600] or "Здравствуйте."
        t = time.perf_counter()
        clip = tts.synth_with(tts.voice, text, rate=max(0.6, min(req.rate, 1.6)))
        return Response(_wav_bytes(clip.samples, clip.sr), media_type="audio/wav", headers={
            "X-Synth-Ms": f"{(time.perf_counter() - t) * 1000:.0f}", "X-Audio-Sec": f"{clip.seconds:.2f}",
            "Cache-Control": "no-store"})

    @app.get("/api/clip/{pack_id}/{reaction}/{index}")
    def clip(pack_id: str, reaction: str, index: int) -> Response:
        files = pack(pack_id).files.get(reaction) or []
        if not 0 <= index < len(files):
            raise HTTPException(404, "Нет такой реплики")
        c = pack(pack_id).clip(files[index])
        return Response(_wav_bytes(c.samples, c.sr), media_type="audio/wav", headers={"Cache-Control": "max-age=3600"})

    @app.post("/api/select")
    def select(req: PackRequest) -> dict:
        if req.pack != "none" and req.pack not in PACK_IDS:
            raise HTTPException(404, "Неизвестный пакет")
        tts.rate = max(0.6, min(req.rate, 1.6))
        save_override("voice.pack", req.pack)
        save_override("tts.rate", tts.rate)
        if on_pack:
            on_pack(req.pack)
        return {"ok": True, "pack": req.pack, "rate": tts.rate}

    return app


class VoiceLabServer:
    """Runs uvicorn in a daemon thread inside the assistant process."""

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        import uvicorn

        config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="voicelab", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.should_exit = True
