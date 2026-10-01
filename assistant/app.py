"""Entry point: python -m assistant [--no-ui] [--text] [--voicelab] [--list-devices]."""
from __future__ import annotations

import argparse
import asyncio
import logging
import msvcrt
import os
import sys
import threading
import webbrowser

from assistant.config import Settings, load_settings
from assistant.core import ConsoleUi, State
from assistant.log import setup_logging
from assistant.paths import DATA_DIR

log = logging.getLogger("app")


def _single_instance(wait_sec: float = 0) -> object | None:
    """Keeps a lock on data/app.lock; returns None if another instance holds it (after waiting, on restart)."""
    import time

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_sec
    while True:
        f = open(DATA_DIR / "app.lock", "a+")
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return f
        except OSError:
            f.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)


def _start_hotkeys(assistant, cfg: Settings):
    from assistant import winutil
    from assistant.hotkeys import Hotkeys

    keys = Hotkeys()
    keys.on_press(cfg.ui.hotkey_listen, lambda: assistant._threadsafe(assistant.listen_now))
    keys.on_press(cfg.ui.hotkey_mute, lambda: assistant._threadsafe(assistant.toggle_mute))
    keys.on_press(cfg.ui.hotkey_screen, lambda: assistant.submit(assistant.handle_text("что на экране")))
    keys.on_hold(cfg.ui.hotkey_dictation,
                 start=lambda: assistant._threadsafe(
                     lambda: assistant.start_dictation(until_pause=False, paste=winutil.paste_text)),
                 stop=lambda: assistant._threadsafe(assistant.stop_dictation))
    keys.start()

    def report() -> None:
        if keys.failed:
            assistant.ui.notify("Горячие клавиши", "Не работают (заняты или неверны): " + ", ".join(keys.failed)
                                + ". Поменяйте в Настройках → Клавиши.")
    threading.Timer(2.0, report).start()
    return keys


def _start_voicelab(assistant, cfg: Settings):
    from assistant.voicelab.server import AssistantHost, VoiceLabServer, create_app

    server = VoiceLabServer(create_app(AssistantHost(assistant)), cfg.voicelab.host, cfg.voicelab.port)
    server.start()
    log.info("Настройки: %s", assistant.voicelab_url)
    return server


class CoreThread(threading.Thread):
    """Loads models and runs the assistant event loop."""

    def __init__(self, assistant) -> None:
        super().__init__(name="core", daemon=True)
        self.assistant = assistant
        self.loop: asyncio.AbstractEventLoop | None = None
        self._main: asyncio.Task | None = None
        self.failed: BaseException | None = None

    def run(self) -> None:
        try:
            self.assistant.load_models()
        except BaseException as exc:  # surface to the UI thread
            self.failed = exc
            log.exception("Не удалось загрузить модели")
            self.assistant.ui.set_state(State.ERROR, str(exc))
            self.assistant._ready.set()
            return
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._main = self.loop.create_task(self.assistant.run())
        try:
            self.loop.run_until_complete(self._main)
        except asyncio.CancelledError:
            pass
        finally:
            self.loop.run_until_complete(self.assistant.shutdown())
            self.loop.close()

    def stop(self) -> None:
        if self.loop and self._main:
            self.loop.call_soon_threadsafe(self._main.cancel)
        self.join(timeout=5)


def _text_console(assistant) -> None:
    """--text: type requests instead of speaking."""
    assistant.wait_ready()
    print("Вводите запросы (пустая строка — выход):", flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            break
        assistant.submit(assistant.handle_text(line))
    assistant.on_exit()


def main() -> None:
    parser = argparse.ArgumentParser(prog="assistant", description="Джарвис — голосовой ассистент")
    parser.add_argument("--no-ui", action="store_true", help="без трея и окон (консоль)")
    parser.add_argument("--text", action="store_true", help="вводить запросы текстом, микрофон не используется")
    parser.add_argument("--voicelab", action="store_true", help="только страница выбора голоса")
    parser.add_argument("--list-devices", action="store_true", help="показать аудиоустройства")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--wait-lock", action="store_true", help=argparse.SUPPRESS)  # used by restart
    args = parser.parse_args()

    setup_logging(args.verbose)
    cfg = load_settings()
    from assistant.log import set_log_phrases

    set_log_phrases(cfg.privacy.log_phrases)

    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return
    if args.voicelab:
        return _voicelab_only(cfg)

    lock = _single_instance(wait_sec=20 if args.wait_lock else 0)
    if lock is None:
        log.error("Джарвис уже запущен (см. значок в трее).")
        sys.exit(1)

    from assistant.assistant import Assistant

    headless = args.no_ui or args.text
    if headless:
        ui = ConsoleUi()
        assistant = Assistant(cfg, ui, use_mic=not args.text)
        core = CoreThread(assistant)
        done = threading.Event()
        assistant.on_exit = done.set
        core.start()
        _start_voicelab(assistant, cfg)
        if not args.text:
            _start_hotkeys(assistant, cfg)
        if args.text:
            threading.Thread(target=_text_console, args=(assistant,), daemon=True).start()
        try:
            while not done.wait(0.5):
                if not core.is_alive():
                    break
        except KeyboardInterrupt:
            pass
        core.stop()
        return

    from PySide6.QtWidgets import QApplication

    from assistant.ui.qt_ui import QtUi

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setApplicationName("Джарвис")
    ui = QtUi(cfg.explain.linger_sec)
    assistant = Assistant(cfg, ui)
    core = CoreThread(assistant)

    def quit_app() -> None:
        ui.tray.hide()
        core.stop()
        qapp.quit()

    ui.quit_requested.connect(quit_app)
    assistant.on_exit = ui.quit_requested.emit  # safe from any thread
    ui.attach(assistant, quit_app)
    core.start()
    _start_voicelab(assistant, cfg)
    _start_hotkeys(assistant, cfg)
    code = qapp.exec()
    if core.is_alive():
        core.stop()
    sys.exit(code)


def _voicelab_only(cfg: Settings) -> None:
    import uvicorn

    from assistant.tts.manager import TtsManager
    from assistant.voicelab.server import Host, create_app

    tts = TtsManager(cfg.tts.voice, cfg.tts.rate, clone_nfe=cfg.tts.clone_nfe)
    url = f"http://{cfg.voicelab.host}:{cfg.voicelab.port}/"
    log.info("Настройки: %s", url)
    threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(Host(cfg, tts)), host=cfg.voicelab.host, port=cfg.voicelab.port, log_level="warning")
