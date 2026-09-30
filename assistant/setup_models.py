"""Downloads every model the default config needs: python -m assistant.setup_models"""
from __future__ import annotations

import logging
import sys
import zipfile

from assistant.config import load_settings
from assistant.downloads import fetch
from assistant.log import setup_logging
from assistant.paths import MODELS_DIR

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"

log = logging.getLogger("setup")


def ensure_vosk(target_rel: str) -> None:
    cfg = load_settings()
    target = cfg.resolve(target_rel)
    if target.exists():
        return
    archive = fetch(VOSK_URL, MODELS_DIR / "vosk-model-small-ru-0.22.zip", what="Vosk small-ru (45 МБ)")
    with zipfile.ZipFile(archive) as z:
        z.extractall(target.parent)
    archive.unlink()


def main() -> int:
    setup_logging()
    cfg = load_settings()
    try:
        from assistant.audio.vad import SileroVad

        SileroVad()
        ensure_vosk(cfg.wake.model)
        from assistant.stt import create_stt

        create_stt(cfg.stt.engine, cfg.stt.model, cfg.stt.quantization)
        from assistant.tts.manager import TtsManager

        TtsManager(cfg.tts.voice).preload()
        from assistant.voicepack import download_packs

        download_packs()
    except Exception:
        log.exception("Ошибка загрузки моделей")
        return 1
    log.info("Все модели на месте: %s", MODELS_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
