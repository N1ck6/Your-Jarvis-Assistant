"""Settings page (http://127.0.0.1:8770): parameters, folders, voice and reactions, AI budget, scenarios,
learned phrases, API keys. Runs inside the assistant process or standalone (python -m assistant --voicelab)."""
from __future__ import annotations

import base64
import io
import logging
import os
import subprocess
import threading
import time
import tomllib
import wave
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ValidationError

from assistant.config import Settings, model_value_map, save_override, set_env_key, update_in_place, validate_changes
from assistant.settings_schema import RESTART_KEYS, schema
from assistant.tts.catalog import VOICES, get_voice
from assistant.tts.manager import TtsManager
from assistant.voicepack import PACK_IDS, VoicePack

log = logging.getLogger("settings")
STATIC = Path(__file__).parent / "static"
REACTION_TITLES = {
    "greet": "Запуск", "greet_morning": "Утро", "greet_day": "День", "greet_evening": "Вечер", "greet_night": "Ночь",
    "reply": "На «Джарвис»", "ok": "Выполнено", "not_found": "Не понял", "thanks": "На «спасибо»",
    "goodbye": "Выход", "joke": "Шутки", "stupid": "На грубость", "game_mode": "Игровой режим",
}
KEY_NAMES = ("GROQ_API_KEY", "GEMINI_API_KEY")


class Host:
    """What the page needs from the running assistant. The standalone page uses this base class."""

    def __init__(self, cfg: Settings, tts: TtsManager) -> None:
        self.cfg = cfg
        self.tts = tts
        self.can_restart = False

    def apply_settings(self, changes: dict[str, Any]) -> list[str]:
        new = validate_changes(changes)
        for key, value in changes.items():
            save_override(key, value)
        update_in_place(self.cfg, new)
        self.tts.rate = self.cfg.tts.rate
        return [k for k in changes if k in RESTART_KEYS]

    def restart(self) -> None:
        raise HTTPException(400, "Перезапуск доступен, когда страница открыта из работающего Джарвиса")

    def phrasebook(self):
        from assistant.router import PHRASEBOOK, _JsonStore

        return _JsonStore(PHRASEBOOK, 1000)

    def ai_status(self) -> dict[str, Any]:
        from assistant.llm.hub import LlmHub

        hub = LlmHub(self.cfg.llm)
        return {"status": hub.status(), "today": hub.usage.today(), "limits": self.cfg.llm.daily_limits}


class AssistantHost(Host):
    def __init__(self, app) -> None:
        super().__init__(app.cfg, app.tts)
        self.app = app
        self.can_restart = True

    def apply_settings(self, changes: dict[str, Any]) -> list[str]:
        return self.app.apply_settings(changes)

    def restart(self) -> None:
        threading.Timer(0.5, self.app.restart).start()

    def phrasebook(self):
        return self.app.brain.router.phrasebook

    def ai_status(self) -> dict[str, Any]:
        hub = self.app.llm
        return {"status": hub.status(), "today": hub.usage.today(), "limits": self.cfg.llm.daily_limits}


# ---------------------------------------------------------------- request bodies
class SynthRequest(BaseModel):
    text: str
    rate: float = 1.0
    voice_id: str = ""


class PackRequest(BaseModel):
    pack: str
    rate: float = 1.0


class ChangesRequest(BaseModel):
    changes: dict[str, Any]


class FolderRequest(BaseModel):
    initial: str = ""


class TextRequest(BaseModel):
    text: str


class KeyRequest(BaseModel):
    name: str
    value: str


def _wav_bytes(samples: np.ndarray, sr: int) -> bytes:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


_PICK_FOLDER_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Windows.Forms.FolderBrowserDialog
$f.Description = 'Выберите папку для Джарвиса'
$f.ShowNewFolderButton = $false
if ($env:JARVIS_INITIAL -and (Test-Path $env:JARVIS_INITIAL)) { $f.SelectedPath = $env:JARVIS_INITIAL }
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false}
if ($f.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::OutputEncoding = [Text.Encoding]::UTF8
  Write-Output $f.SelectedPath
}
"""


def pick_folder(initial: str) -> str | None:
    """Native Windows folder dialog in a separate STA PowerShell process (safe from any thread)."""
    encoded = base64.b64encode(_PICK_FOLDER_PS.encode("utf-16-le")).decode()
    env = dict(os.environ, JARVIS_INITIAL=initial)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded], capture_output=True,
                             timeout=300, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return None
    path = out.stdout.decode("utf-8", "replace").strip()
    return path or None


def create_app(host: Host) -> FastAPI:
    app = FastAPI(title="Jarvis Settings", docs_url=None, redoc_url=None)
    packs: dict[str, VoicePack] = {}
    cfg = host.cfg
    port = cfg.voicelab.port
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    @app.middleware("http")
    async def local_only(request, call_next):
        """Only this page may talk to the API: blocks DNS rebinding (Host) and other websites (Origin)."""
        from fastapi.responses import PlainTextResponse

        origin = request.headers.get("origin")
        if request.headers.get("host") not in allowed_hosts or (origin and origin not in allowed_origins):
            return PlainTextResponse("forbidden", status_code=403)
        if request.method == "POST" and "application/json" not in request.headers.get("content-type", ""):
            return PlainTextResponse("json only", status_code=415)  # no "simple" cross-site form posts
        return await call_next(request)

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

    # ------------------------------------------------------------- voice & reactions
    @app.get("/api/state")
    def state() -> dict:
        out = []
        for pid in PACK_IDS:
            p = VoicePack.load(pid)
            if p is not None:
                out.append({"id": pid, "title": p.title, "reactions": [
                    {"key": k, "title": REACTION_TITLES.get(k, k), "count": len(v)} for k, v in p.files.items()]})
        return {"voices": [v.to_dict() for v in VOICES], "voice": cfg.tts.voice, "active_voice": host.tts.voice.id,
                "rate": host.tts.rate, "pack": cfg.voice.pack, "packs": out, "can_restart": host.can_restart}

    @app.post("/api/synth")
    def synth(req: SynthRequest) -> Response:
        text = req.text.strip()[:600] or "Здравствуйте."
        try:
            spec = get_voice(req.voice_id) if req.voice_id else host.tts.voice
            t = time.perf_counter()
            host.tts.engine(spec.engine).load(spec.voice)
            load_ms = (time.perf_counter() - t) * 1000
            t = time.perf_counter()
            clip = host.tts.synth_with(spec, text, rate=max(0.6, min(req.rate, 1.6)))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            log.exception("Синтез")
            raise HTTPException(502, f"Голос недоступен: {exc}") from exc
        return Response(_wav_bytes(clip.samples, clip.sr), media_type="audio/wav", headers={
            "X-Synth-Ms": f"{(time.perf_counter() - t) * 1000:.0f}", "X-Load-Ms": f"{load_ms:.0f}",
            "X-Audio-Sec": f"{clip.seconds:.2f}", "Cache-Control": "no-store"})

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
        host.apply_settings({"voice.pack": req.pack, "tts.rate": max(0.6, min(req.rate, 1.6))})
        return {"ok": True, "pack": req.pack}

    # ------------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings() -> dict:
        voice_options = [[v.id, v.title] for v in VOICES]
        return {"sections": schema(voice_options), "values": model_value_map(cfg), "can_restart": host.can_restart}

    @app.post("/api/settings")
    def post_settings(req: ChangesRequest) -> dict:
        try:
            restart = host.apply_settings(req.changes)
        except ValidationError as exc:
            msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])
            raise HTTPException(400, f"Неверное значение — {msg}") from exc
        return {"ok": True, "restart": restart}

    @app.post("/api/pick-folder")
    def post_pick_folder(req: FolderRequest) -> dict:
        initial = str(cfg.resolve(req.initial)) if req.initial else ""
        return {"path": pick_folder(initial)}

    @app.get("/api/devices")
    def devices() -> dict:
        import sounddevice as sd

        ins, outs = [["", "Системный по умолчанию"]], [["", "Системные по умолчанию"]]
        seen_in, seen_out = set(), set()
        for d in sd.query_devices():
            name = d["name"]
            if d["max_input_channels"] > 0 and name not in seen_in:
                seen_in.add(name)
                ins.append([name, name])
            if d["max_output_channels"] > 0 and name not in seen_out:
                seen_out.add(name)
                outs.append([name, name])
        return {"inputs": ins, "outputs": outs}

    @app.get("/api/ollama")
    def ollama_models() -> dict:
        try:
            import ollama

            models = [m.model for m in ollama.Client(host=cfg.llm.local.host).list().models]
        except Exception as exc:
            return {"models": [], "error": f"Ollama недоступна: {exc}"}
        return {"models": models}

    # ------------------------------------------------------------- scenarios / phrases / AI / keys
    @app.get("/api/scenarios")
    def get_scenarios() -> dict:
        from assistant.skills.scenarios import scenarios_file

        path = scenarios_file()
        return {"text": path.read_text(encoding="utf-8"), "path": str(path)}

    @app.post("/api/scenarios")
    def post_scenarios(req: TextRequest) -> dict:
        from assistant.skills.scenarios import scenarios_file

        try:
            data = tomllib.loads(req.text)
        except tomllib.TOMLDecodeError as exc:
            raise HTTPException(400, f"Ошибка в файле: {exc}") from exc
        bad = [s.get("name", "?") for s in data.get("scenario", []) if not s.get("phrases")]
        if bad:
            raise HTTPException(400, f"У сценария нет фраз: {', '.join(bad)}")
        scenarios_file().write_text(req.text, encoding="utf-8")
        return {"ok": True, "count": len(data.get("scenario", []))}

    @app.get("/api/phrasebook")
    def get_phrasebook() -> dict:
        store = host.phrasebook()
        items = sorted(store.data.items(), key=lambda kv: -kv[1].get("ts", 0))
        return {"items": [{"phrase": k, "commands": v["commands"], "uses": v.get("uses", 0)} for k, v in items]}

    @app.post("/api/phrasebook/delete")
    def delete_phrase(req: TextRequest) -> dict:
        store = host.phrasebook()
        store.data.pop(req.text, None)
        store.save()
        return {"ok": True}

    @app.get("/api/ai")
    def ai() -> dict:
        return host.ai_status()

    @app.get("/api/keys")
    def get_keys() -> dict:
        return {name: bool(os.environ.get(name, "").strip()) for name in KEY_NAMES}

    @app.post("/api/keys")
    def post_key(req: KeyRequest) -> dict:
        if req.name not in KEY_NAMES:
            raise HTTPException(400, "Неизвестный ключ")
        set_env_key(req.name, req.value)
        return {"ok": True, "restart": True}

    @app.post("/api/restart")
    def restart() -> dict:
        host.restart()
        return {"ok": True}

    return app


class VoiceLabServer:
    """Runs uvicorn in a daemon thread inside the assistant process."""

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        import uvicorn

        config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="settings-web", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.should_exit = True
