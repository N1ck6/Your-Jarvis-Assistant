"""Orchestrator: wires microphone, STT, brain, speech and UI together.

Threads: Qt main thread (UI), one asyncio loop thread (everything here),
the listener thread (VAD/wake) and PortAudio callback threads.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable

from assistant.audio import earcons
from assistant.audio.music import MusicPlayer
from assistant.audio.listener import ListenerCore, ListenerEvents, Microphone, Mode, Utterance
from assistant.audio.player import AudioClip, Player
from assistant.audio.vad import SileroVad
from assistant.audio.wake import VoskWake
from assistant.brain import Brain, Dialog
from assistant.config import Settings
from assistant.core import Card, Deck, State, UiPort
from assistant.llm.hub import LlmHub
from assistant.nlu import find_wake, normalize_command, strip_wake
from assistant.skills.base import Reply, load_skills
from assistant.speech import Speaker
from assistant.stt import create_stt
from assistant.tts.manager import TtsManager
from assistant.voicepack import VoicePack, load_pack

log = logging.getLogger("assistant")


class Assistant:
    def __init__(self, cfg: Settings, ui: UiPort, *, use_mic: bool = True) -> None:
        self.cfg = cfg
        self.ui = ui
        self.use_mic = use_mic
        self.loop: asyncio.AbstractEventLoop | None = None
        self.state = State.IDLE
        self.voicelab_url = f"http://{cfg.voicelab.host}:{cfg.voicelab.port}/"
        self.on_exit: Callable[[], None] = lambda: None

        self.dialog = Dialog(cfg.assistant.dialog_ttl_sec, cfg.assistant.dialog_max_turns)
        self.llm = LlmHub(cfg.llm)
        self.tts = TtsManager(cfg.tts.voice, cfg.tts.rate, clone_nfe=cfg.tts.clone_nfe)
        self.pack: VoicePack | None = load_pack(cfg.voice.pack)
        self.player = Player(cfg.audio.output_device, cfg.tts.volume)
        self.music = MusicPlayer(cfg.music.volume, cfg.music.duck_volume)
        self.speaker = Speaker(self.tts, self.player, on_start=self._on_speech_start, on_end=self._on_speech_end)
        self.skills = load_skills(cfg.skills.enabled)
        for skill in self.skills:
            skill.setup(self)
        self.brain = Brain(self, self.skills)

        self.stt = None
        self.mic: Microphone | None = None
        self.listener: ListenerCore | None = None
        self._task: asyncio.Task | None = None
        self._ready = threading.Event()
        self._dictation_paste: Callable[[str], None] | None = None
        self._muted_before_dictation = False

    # ------------------------------------------------------------------ startup
    def load_models(self) -> None:
        """Slow part (seconds): run before the loop starts."""
        t = time.perf_counter()
        self.stt = create_stt(self.cfg.stt.engine, self.cfg.stt.model, self.cfg.stt.quantization)
        self.stt.warmup()
        if self.tts.voice.engine == "clone":
            # The cloned voice needs ~40 s to load: speak with Silero meanwhile, switch when it is ready.
            target = self.tts.voice.id
            self.tts.set_voice(self.tts.fallback_id)
            threading.Thread(target=self._switch_voice, args=(target,), name="clone-load", daemon=True).start()
        try:
            self.tts.preload()
            self.tts.synth("Проверка.")
        except Exception:
            log.exception("Голос %s не загрузился, переключаюсь на %s", self.tts.voice.id, self.tts.fallback_id)
            self.tts.set_voice(self.tts.fallback_id)
        if self.use_mic:
            wake_cfg = self.cfg.wake
            events = ListenerEvents(
                on_wake=lambda: self._threadsafe(self._on_wake),
                on_stop_word=lambda w, audio: self._threadsafe(self._on_stop_word, w, audio),
                on_speech_start=lambda: self._threadsafe(self._set_state, State.LISTENING),
                on_utterance=lambda u: self._threadsafe(self._on_utterance, u),
                on_await_timeout=lambda: self._threadsafe(self._on_await_timeout),
            )
            self.listener = ListenerCore(
                SileroVad(self.cfg.audio.sample_rate),
                VoskWake(self.cfg.resolve(wake_cfg.model), wake_cfg.phrases, wake_cfg.decoys, wake_cfg.stop_words),
                events, sr=self.cfg.audio.sample_rate, vad_threshold=self.cfg.audio.vad_threshold,
                end_silence_ms=self.cfg.audio.end_silence_ms, max_utterance_sec=self.cfg.audio.max_utterance_sec,
                wake_grace_ms=self.cfg.audio.wake_grace_ms,
                pre_roll_ms=self.cfg.audio.pre_roll_ms)
            self.mic = Microphone(self.listener, self.cfg.audio.input_device, self.cfg.audio.sample_rate)
        log.info("Модели загружены за %.1f с", time.perf_counter() - t)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        for skill in self.skills:
            try:
                await skill.start()
            except Exception:
                log.exception("Модуль %s не стартовал", skill.name)
        asyncio.create_task(self._warmup())
        log.info("Провайдеры: %s", self.llm.status())
        if self.mic:
            self.mic.start()
        self._set_state(State.IDLE)
        self._ready.set()
        log.info("Готов. Скажите «%s» и команду.", self.cfg.assistant.name)
        if self.cfg.voice.greet_on_start and self.pack:
            clip = self.pack.greeting()
            if clip:
                await self.speaker.play_clip(clip)
                self._set_state(State.IDLE)
        await asyncio.Event().wait()  # run forever; cancelled on shutdown

    async def _warmup(self) -> None:
        from assistant import nlu

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, nlu._morph)
        if self.state is State.IDLE:
            self.ui.set_state(State.IDLE, "локальная модель загружается…")
        await self.llm.local.warmup()
        if self.state is State.IDLE:
            self.ui.set_state(State.IDLE)

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    async def shutdown(self) -> None:
        self.music.stop()
        if self.mic:
            self.mic.stop()
        self.speaker.shutdown()
        for skill in self.skills:
            try:
                await skill.stop()
            except Exception:
                log.exception("stop() %s", skill.name)

    # ------------------------------------------------------------------ helpers
    def _threadsafe(self, func, *args) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(func, *args)

    def submit(self, coro) -> None:
        """Run a coroutine on the assistant loop from any thread (UI buttons, hotkeys)."""
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _set_state(self, state: State, detail: str = "") -> None:
        self.state = state
        # Music steps aside while Jarvis listens, thinks or talks.
        self.music.duck(state in (State.LISTENING, State.THINKING, State.SPEAKING))
        self.ui.set_state(state, detail)

    def _earcon(self, clip: AudioClip) -> None:
        if self.cfg.audio.earcons:
            self.player.effect(clip, self.cfg.audio.earcon_volume)

    def _on_speech_start(self) -> None:
        if self.listener:
            self.listener.assistant_speaking = True
        self._set_state(State.SPEAKING)

    def _on_speech_end(self) -> None:
        if self.listener:
            self.listener.assistant_speaking = False

    def apply_settings(self, changes: dict) -> list[str]:
        """Saves changes, updates the live settings; returns the keys that need a restart."""
        from assistant.config import save_override, update_in_place, validate_changes
        from assistant.settings_schema import RESTART_KEYS

        new = validate_changes(changes)  # raises on bad values, nothing is saved then
        for key, value in changes.items():
            save_override(key, value)
        update_in_place(self.cfg, new)
        self.tts.rate = self.cfg.tts.rate
        self.player.volume = self.cfg.tts.volume
        self.music.volume = self.cfg.music.volume
        self.music.duck_volume = self.cfg.music.duck_volume
        if "voice.pack" in changes:
            self.set_voice_pack(self.cfg.voice.pack)
        if "tts.voice" in changes:
            threading.Thread(target=self._switch_voice, args=(self.cfg.tts.voice,), daemon=True).start()
        self.brain.router.reset_catalog()
        for skill in self.skills:
            if hasattr(skill, "_stamp"):
                skill._stamp = -1  # re-scan folders (music) on next use
        log.info("Настройки изменены: %s", ", ".join(changes))
        return [k for k in changes if k in RESTART_KEYS]

    def _switch_voice(self, voice_id: str) -> None:
        try:
            self.tts.set_voice(voice_id)
        except Exception:
            log.exception("Голос %s не загрузился", voice_id)
            self.ui.notify("Голос", f"Не удалось загрузить голос {voice_id}, остаётся прежний")

    def restart(self) -> None:
        """Starts a fresh instance (it waits for this one to release the lock) and exits."""
        import subprocess
        import sys

        from assistant.paths import ROOT

        exe = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        cmd = [str(exe) if exe.exists() else sys.executable, "-m", "assistant", "--wait-lock"]
        subprocess.Popen(cmd, cwd=str(ROOT), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        log.info("Перезапуск")
        self.on_exit()

    def set_voice_pack(self, pack_id: str) -> None:
        self.pack = load_pack(pack_id)
        self.cfg.voice.pack = pack_id

    async def react(self, reaction: str) -> bool:
        """Plays a recorded Jarvis reaction. False if the pack has none (caller then speaks)."""
        clip = self.pack.pick(reaction) if self.pack else None
        if clip is None:
            return False
        await self.speaker.play_clip(clip, reaction)
        return True

    # ------------------------------------------------------------------ control API (used by skills/UI)
    def interrupt(self) -> None:
        """Stop talking and drop the running request (except the caller itself)."""
        self.speaker.stop()
        task = self._task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def set_muted(self, muted: bool) -> None:
        if not self.mic:
            return
        if muted:
            self.interrupt()
            self.mic.pause()
            self._set_state(State.MUTED)
        else:
            self.mic.resume()
            self._set_state(State.IDLE)

    def toggle_mute(self) -> None:
        if self.mic:
            self.set_muted(not self.mic.paused)

    def listen_now(self) -> None:
        """Hotkey / tray: start listening without the wake word."""
        if not self.listener:
            return
        if self.mic and self.mic.paused:
            self.set_muted(False)
        self.interrupt()
        self._earcon(earcons.WAKE)
        self.listener.to_await(self.cfg.audio.await_command_sec)
        self._set_state(State.LISTENING)

    def request_exit(self) -> None:
        async def bye() -> None:
            if not await self.react("goodbye"):
                await self.speaker.say(f"Отключаюсь, {self.cfg.assistant.address}.")
            self.on_exit()

        self.submit(bye())

    async def announce(self, text: str, cue: AudioClip | None = None) -> None:
        """Unprompted speech (timers). Waits for the current answer to finish."""
        if cue is not None:
            self._earcon(cue)
            await asyncio.sleep(cue.seconds + 0.1)
        await self.speaker.say(text)
        if self.state is State.SPEAKING:
            self._set_state(State.IDLE)

    # ------------------------------------------------------------------ dictation
    def start_dictation(self, until_pause: bool, paste: Callable[[str], None]) -> bool:
        """Records speech and hands the text to `paste`. Hold-hotkey mode ends with stop_dictation()."""
        if not self.listener or not self.mic:
            return False
        self._muted_before_dictation = self.mic.paused
        if self.mic.paused:
            self.mic.resume()
        self.interrupt()
        self._dictation_paste = paste
        self.listener.start_dictation(until_pause)
        self._set_state(State.LISTENING, "диктовка")
        return True

    def stop_dictation(self) -> None:
        if self.listener:
            self.listener.stop_dictation()

    async def _finish_dictation(self, utt: Utterance) -> None:
        paste, self._dictation_paste = self._dictation_paste, None
        if self._muted_before_dictation and self.mic:
            self.mic.pause()
        if utt.audio.size == 0 or paste is None:
            self._set_state(State.MUTED if self._muted_before_dictation else State.IDLE)
            return
        self._set_state(State.THINKING, "распознаю диктовку")
        t = time.perf_counter()
        text = await asyncio.get_running_loop().run_in_executor(None, self.stt.transcribe_long, utt.audio)
        log.info("Диктовка %.1f с за %.0f мс: %s", utt.audio.size / 16000, (time.perf_counter() - t) * 1000, text[:120])
        if text:
            await asyncio.get_running_loop().run_in_executor(None, paste, text)
            self._earcon(earcons.DONE)
        self._set_state(State.MUTED if self._muted_before_dictation else State.IDLE)

    # ------------------------------------------------------------------ listener events
    def _on_wake(self) -> None:
        # Jarvis saying his own name through the speakers is not a wake word.
        if self.speaker.speaking and find_wake(self.speaker.recent_text(), self.cfg.wake.phrases, 85):
            log.info("Своё имя в собственной речи, игнорирую")
            if self.listener:
                self.listener.to_wait()
            return
        self.interrupt()
        self._earcon(earcons.WAKE)
        self._set_state(State.LISTENING)

    def _on_stop_word(self, word: str, audio) -> None:
        asyncio.create_task(self._verify_stop_word(word, audio))

    async def _verify_stop_word(self, word: str, audio) -> None:
        """Vosk with a tiny grammar maps any speech to its nearest word, and the mic hears Jarvis himself.
        So the stop word must be confirmed by the real recognizer and must not be in what Jarvis just said."""
        if not self.speaker.speaking:
            return
        text = await asyncio.get_running_loop().run_in_executor(None, self.stt.transcribe, audio)
        heard = normalize_command(text, strip_polite=False).split()
        own = set(normalize_command(self.speaker.recent_text(), strip_polite=False).split())
        stops = [w for w in heard if w in self.cfg.wake.stop_words and w not in own]
        if not stops:
            log.info("Стоп-слово «%s» не подтвердилось (слышно: «%s»), продолжаю", word, text[:80])
            return
        log.info("Прерван словом «%s», слушаю команду", stops[0])
        self.interrupt()
        self._earcon(earcons.WAKE)
        if self.listener:
            self.listener.to_await(self.cfg.audio.await_command_sec)
        self._set_state(State.LISTENING, "слушаю новую команду")

    def _on_await_timeout(self) -> None:
        if self.state in (State.LISTENING,):
            self._set_state(State.IDLE)

    def _on_utterance(self, utt: Utterance) -> None:
        if utt.source == "dictation":
            asyncio.create_task(self._finish_dictation(utt))
            return
        self._task = asyncio.create_task(self._process_audio(utt))

    async def _process_audio(self, utt: Utterance) -> None:
        self._set_state(State.THINKING)
        t = time.perf_counter()
        text = await asyncio.get_running_loop().run_in_executor(None, self.stt.transcribe, utt.audio)
        log.info("Распознано за %.0f мс: «%s»", (time.perf_counter() - t) * 1000, text)
        wake = self.cfg.wake
        if utt.source == "wake" and wake.verify_with_stt and text and not find_wake(text, wake.phrases, wake.verify_ratio):
            log.info("Ложное срабатывание кодового слова, игнорирую")
            self._set_state(State.IDLE)
            return
        query = strip_wake(text, wake.phrases, wake.verify_ratio)
        if len(query) < 2:
            if utt.source == "wake":
                # Only "Джарвис" was said: "Слушаю, сэр" and wait for the command.
                if self.cfg.voice.reply_on_bare_wake:
                    await self.react("reply")
                self._set_state(State.LISTENING)
                if self.listener:
                    self.listener.to_await(self.cfg.audio.await_command_sec)
            else:
                self._set_state(State.IDLE)
            return
        await self.handle_text(query)

    # ------------------------------------------------------------------ main request path
    async def handle_text(self, query: str) -> None:
        """Entry point for recognized speech and typed questions."""
        if asyncio.current_task() is not self._task:
            self.interrupt()
            self._task = asyncio.current_task()
        self._set_state(State.THINKING, query)
        log.info("Запрос: %s", query)
        started = time.perf_counter()
        listen_after = True
        try:
            reply = await self.brain.handle(query)
            listen_after = await self._deliver(reply, started)
            while self.brain.extra:
                listen_after = await self._deliver(self.brain.extra.pop(0), started)
            while self.brain.deferred:
                skill, intent = self.brain.deferred.pop(0)
                listen_after = await self._deliver(await skill.handle(intent), started)
        except asyncio.CancelledError:
            log.info("Запрос отменён")
            self.brain.deferred.clear()
            self.brain.extra.clear()
            raise
        except Exception:
            log.exception("Ошибка обработки запроса")
            self._earcon(earcons.ERROR)
            await self.speaker.say("Что-то пошло не так, подробности в логе.")
        if self._task is asyncio.current_task():
            self._after_answer(listen_after)

    async def _deliver(self, reply: Reply, started: float) -> bool:
        if reply.spoken:
            return reply.listen_after
        text = ""
        use_reaction = reply.reaction and (reply.reaction != "ok" or self.cfg.voice.ok_for_actions)
        if use_reaction and await self.react(reply.reaction):
            text = f"[{reply.reaction}] {reply.speech}".strip()
        elif reply.stream is not None:
            first = True

            async def timed():
                nonlocal first
                async for delta in reply.stream:
                    if first:
                        first = False
                        log.info("Первый токен через %.2f с (%s)", time.perf_counter() - started,
                                 self.llm.last_provider or "-")
                    yield delta

            text = await self.speaker.speak(timed())
        elif reply.speech:
            text = await self.speaker.say(reply.speech)
        if text and not text.startswith("[") and len(text) > self.cfg.ui.card_threshold_chars:
            self.ui.show_deck(Deck(title="Ответ", cards=[Card("", text, "")], done=True))
        log.info("Ответ за %.2f с: %s", time.perf_counter() - started, text[:200])
        return reply.listen_after or reply.confirm is not None

    def _after_answer(self, listen_after: bool) -> None:
        if self.listener and listen_after and self.mic and not self.mic.paused:
            # Hot window: follow-up without the wake word. Short delay skips the speaker echo tail.
            def open_window() -> None:
                if self.listener and self.listener.mode is Mode.WAIT and self.state is not State.MUTED:
                    self.listener.to_await(self.cfg.assistant.hot_window_sec)
                    self._set_state(State.LISTENING, "можно без «Джарвис»")
            asyncio.get_running_loop().call_later(0.25, open_window)
        elif self.state is not State.MUTED:
            self._set_state(State.IDLE)
