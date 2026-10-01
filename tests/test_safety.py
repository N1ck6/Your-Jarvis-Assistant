"""Read-only guarantees, settings validation, budget, music player."""
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from assistant import cmdsandbox, fsaccess
from assistant.config import load_settings, update_in_place, validate_changes


@pytest.fixture
def cfg(tmp_path):
    c = load_settings()
    c.files.allowed_dirs = [str(tmp_path)]
    c.music.dir = str(tmp_path)
    c.notes.dir = str(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "report.pdf").write_bytes(b"%PDF")
    (tmp_path / "song.mp3").write_bytes(b"x")
    return c


@pytest.mark.parametrize("command", [
    "del C:\\Windows\\win.ini", "dir C:\\ > out.txt", "dir C:\\ & del x", "type x | more", "copy a b",
    "ipconfig /release", "powershell Remove-Item x", "rd /s /q C:\\", "dir %USERPROFILE%", "echo hi",
    "format c:", "dir C:\\Windows",
])
def test_cmd_refused(cfg, command):
    with pytest.raises(cmdsandbox.Refused):
        cmdsandbox.check(cfg, command)


def test_cmd_allowed(cfg, tmp_path):
    cmdsandbox.check(cfg, f'dir /b "{tmp_path}"')
    cmdsandbox.check(cfg, "ipconfig")
    cmdsandbox.check(cfg, f'findstr /s "PDF" "{tmp_path}\\*.pdf"')
    out = cmdsandbox.run(cfg, f'dir /b /s "{tmp_path}\\*.pdf"')
    assert "report.pdf" in out
    assert cmdsandbox.run(cfg, "del x").startswith("Отказано")


def test_fsaccess_bounds(cfg, tmp_path):
    assert fsaccess.is_allowed(cfg, tmp_path / "docs" / "report.pdf")
    assert not fsaccess.is_allowed(cfg, Path("C:/Windows/System32"))
    assert fsaccess.resolve_dir(cfg, "папке docs") == tmp_path / "docs"
    assert fsaccess.count(tmp_path, fsaccess.kind_exts("pdf")) == 1
    assert fsaccess.count(tmp_path, None) == 2
    assert [f.path.name for f in fsaccess.find(cfg, "report")] == ["report.pdf"]


def test_settings_validation_and_live_update():
    cfg = load_settings()
    music = cfg.music
    new = validate_changes({"music.volume": 0.3, "llm.daily_limits.groq": 100})
    update_in_place(cfg, new)
    assert cfg.music is music and cfg.music.volume == 0.3   # same object, modules keep working
    assert cfg.llm.daily_limits["groq"] == 100
    with pytest.raises(ValidationError):
        validate_changes({"music.volume": "громко"})


def test_usage_budget(tmp_path, monkeypatch):
    import assistant.llm.usage as usage_mod
    from assistant.config import CloudCfg, LlmCfg
    from assistant.llm.hub import LlmHub

    monkeypatch.setattr(usage_mod, "FILE", tmp_path / "usage.json")
    hub = LlmHub(LlmCfg(daily_limits={"groq": 2}, groq=CloudCfg(models=["m"])))
    hub.providers["groq"].key = "x"
    hub.usage = usage_mod.Usage()
    hub.usage.record("groq", 10, 5)
    assert hub.status()["groq"] == "готов"
    hub.usage.record("groq", 10, 5)
    assert hub.status()["groq"] == "дневной лимит"
    assert hub.usage.today()["groq"] == {"requests": 2, "in": 20, "out": 10}


def test_music_player_queue(tmp_path):
    from assistant.audio.music import MusicPlayer

    files = []
    for i in range(2):
        path = tmp_path / f"t{i}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(np.zeros(44100, np.int16).tobytes())
        files.append(path)
    p = MusicPlayer(volume=0.0)  # silent
    p.play(files)
    time.sleep(0.2)
    assert p.active and p.current == files[0]
    p.pause()
    assert p.state == "paused"
    p.resume()
    p.next()
    assert p.current == files[1]
    p.stop()
    assert not p.active


def test_settings_api_rejects_other_sites():
    from fastapi.testclient import TestClient

    from assistant.tts.manager import TtsManager
    from assistant.voicelab.server import Host, create_app

    cfg = load_settings()
    client = TestClient(create_app(Host(cfg, TtsManager("silero:eugene"))), base_url=f"http://127.0.0.1:{cfg.voicelab.port}")
    assert client.get("/api/keys").status_code == 200
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/settings", json={"changes": {"files.allow_cmd": True}}, headers=evil).status_code == 403
    assert client.get("/api/keys", headers={"Host": "attacker.example:8770"}).status_code == 403
    form = client.post("/api/restart", content="{}", headers={"Content-Type": "text/plain"})
    assert form.status_code == 415


def test_sleep_until_uses_wall_clock(monkeypatch):
    import asyncio
    import time as time_mod

    from assistant import nlu

    fake_now = [1000.0]
    monkeypatch.setattr(time_mod, "time", lambda: fake_now[0])
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)
        fake_now[0] += 1800  # the laptop slept for half an hour during this step

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(nlu.sleep_until(1000.0 + 3600, step=15))
    assert len(slept) == 2  # woke up and fired right after the clock passed, not 3600 s later
