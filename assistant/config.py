"""Settings: config/default.toml merged with user overrides in data/settings.json."""
from __future__ import annotations

import json
import os
import threading
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from assistant.paths import CONFIG_DIR, DATA_DIR, ROOT

DEFAULT_FILE = CONFIG_DIR / "default.toml"
USER_FILE = DATA_DIR / "settings.json"


class AssistantCfg(BaseModel):
    name: str = "Джарвис"
    address: str = "сэр"
    language: str = "ru"
    default_city: str = "Москва"
    hot_window_sec: float = 5.0
    dialog_ttl_sec: float = 180
    dialog_max_turns: int = 6


class AudioCfg(BaseModel):
    input_device: str = ""
    output_device: str = ""
    sample_rate: int = 16000
    vad_threshold: float = 0.5
    end_silence_ms: int = 700
    max_utterance_sec: float = 15.0
    await_command_sec: float = 6.0
    pre_roll_ms: int = 400
    earcons: bool = True
    earcon_volume: float = 0.25


class WakeCfg(BaseModel):
    engine: str = "vosk"
    model: str = "models/vosk-model-small-ru-0.22"
    phrases: list[str] = Field(default_factory=lambda: ["джарвис"])
    decoys: list[str] = Field(default_factory=list)
    stop_words: list[str] = Field(default_factory=lambda: ["стоп"])
    verify_with_stt: bool = True
    verify_ratio: int = 70


class SttCfg(BaseModel):
    engine: str = "gigaam"
    model: str = "gigaam-v3-e2e-rnnt"
    quantization: str = "int8"


class TtsCfg(BaseModel):
    voice: str = "silero:eugene"
    rate: float = 1.0
    volume: float = 1.0


class VoicePackCfg(BaseModel):
    pack: str = "jarvis-remaster"      # recorded reactions; "none" = only synthesized speech
    greet_on_start: bool = True
    reply_on_bare_wake: bool = True    # "Слушаю, сэр" when only the wake word was said
    ok_for_actions: bool = True        # "Слушаюсь, сэр" instead of describing simple actions


class CloudCfg(BaseModel):
    models: list[str] = Field(default_factory=list)
    web_search: bool = True
    max_output_tokens: int = 500


class LocalLlmCfg(BaseModel):
    host: str = "http://127.0.0.1:11434"
    model: str = "qwen3:8b"
    vision_model: str = "qwen3-vl:4b-instruct"
    keep_alive: str = "30m"
    num_ctx: int = 4096
    temperature: float = 0.6


class LlmCfg(BaseModel):
    info_chain: list[str] = Field(default_factory=lambda: ["groq", "gemini", "local"])
    vision_chain: list[str] = Field(default_factory=lambda: ["gemini", "local"])
    local_chain: list[str] = Field(default_factory=lambda: ["local"])
    cooldown_sec: float = 900
    request_timeout_sec: float = 25
    gemini: CloudCfg = Field(default_factory=CloudCfg)
    groq: CloudCfg = Field(default_factory=CloudCfg)
    local: LocalLlmCfg = Field(default_factory=LocalLlmCfg)


class SkillsCfg(BaseModel):
    enabled: list[str] = Field(default_factory=list)


class AppsCfg(BaseModel):
    browser_home: str = "https://ya.ru"
    aliases: dict[str, str] = Field(default_factory=dict)
    translit: dict[str, str] = Field(default_factory=dict)


class NotesCfg(BaseModel):
    dir: str = "data/notes"            # can point to an Obsidian vault folder
    default_list: str = "заметки"


class MusicCfg(BaseModel):
    dir: str = "~/Desktop/music"


class CalendarCfg(BaseModel):
    ics_urls: list[str] = Field(default_factory=list)
    cache_min: int = 10


class BriefingCfg(BaseModel):
    auto_time: str = ""                # "08:30" = say the briefing automatically every day


class FocusCfg(BaseModel):
    work_min: int = 25
    break_min: int = 5
    long_break_min: int = 15
    rounds: int = 4


class SearchCfg(BaseModel):
    default: str = "yandex"
    engines: dict[str, str] = Field(default_factory=dict)


class ExplainCfg(BaseModel):
    provider: str = "local"
    max_cards: int = 5
    linger_sec: float = 25


class UiCfg(BaseModel):
    tray: bool = True
    hotkey_listen: str = "<ctrl>+<alt>+j"
    hotkey_mute: str = "<ctrl>+<alt>+m"
    hotkey_dictation: str = "<ctrl>+<alt>+d"   # hold to dictate
    hotkey_screen: str = "<ctrl>+<alt>+s"
    card_threshold_chars: int = 320


class VoiceLabCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8770


class Settings(BaseModel):
    assistant: AssistantCfg = Field(default_factory=AssistantCfg)
    audio: AudioCfg = Field(default_factory=AudioCfg)
    wake: WakeCfg = Field(default_factory=WakeCfg)
    stt: SttCfg = Field(default_factory=SttCfg)
    tts: TtsCfg = Field(default_factory=TtsCfg)
    voice: VoicePackCfg = Field(default_factory=VoicePackCfg)
    llm: LlmCfg = Field(default_factory=LlmCfg)
    skills: SkillsCfg = Field(default_factory=SkillsCfg)
    apps: AppsCfg = Field(default_factory=AppsCfg)
    explain: ExplainCfg = Field(default_factory=ExplainCfg)
    notes: NotesCfg = Field(default_factory=NotesCfg)
    music: MusicCfg = Field(default_factory=MusicCfg)
    calendar: CalendarCfg = Field(default_factory=CalendarCfg)
    briefing: BriefingCfg = Field(default_factory=BriefingCfg)
    focus: FocusCfg = Field(default_factory=FocusCfg)
    search: SearchCfg = Field(default_factory=SearchCfg)
    ui: UiCfg = Field(default_factory=UiCfg)
    voicelab: VoiceLabCfg = Field(default_factory=VoiceLabCfg)

    def resolve(self, rel: str) -> Path:
        p = Path(rel).expanduser()
        return p if p.is_absolute() else ROOT / p


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_lock = threading.Lock()


def load_user_overrides() -> dict[str, Any]:
    if not USER_FILE.exists():
        return {}
    try:
        return json.loads(USER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    with DEFAULT_FILE.open("rb") as f:
        base = tomllib.load(f)
    return Settings.model_validate(_deep_merge(base, load_user_overrides()))


def save_override(dotted_key: str, value: Any) -> None:
    """Persist one setting, e.g. save_override("tts.voice", "piper:ru_RU-denis-medium")."""
    with _lock:
        data = load_user_overrides()
        node = data
        *parents, leaf = dotted_key.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = USER_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, USER_FILE)


def secret(name: str) -> str:
    return os.environ.get(name, "").strip()
