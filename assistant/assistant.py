"""Orchestrator: wires microphone, STT, brain, speech and UI together.

Threads: Qt main thread (UI), one asyncio loop thread (everything here),
the listener thread (VAD/wake) and PortAudio callback threads.
"""
from __future__ import annotations

import asyncio
import logging
import re
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
from assistant.log import private
from assistant.nlu import find_wake, is_bare_wake, is_echo, normalize_command, strip_echo, strip_wake
from assistant.skills.base import Reply, load_skills
from assistant.speech import Speaker
from assistant.stt import create_stt
from assistant.tts.manager import TtsManager
from assistant.voicepack import VoicePack, load_pack

log = logging.getLogger("assistant")


def resample(samples, sr: int, target: int):
    """Linear resampling: enough for recognizing short recorded clips."""
    import numpy as np

    if sr == target:
        return samples.astype(np.float32)
    n = int(len(samples) * target / sr)
    return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(np.float32)


def answer_card_text(text: str) -> str:
    """A long spoken answer as card paragraphs of two sentences (lists and line breaks are kept)."""
    if "\n" in text.strip():
        return text.strip()
    sentences = re.split(r"(?<=[.!?…])\s+(?=[А-ЯЁA-Z0-9«])", text.strip())
    return "\n".join(" ".join(sentences[i:i + 2]) for i in range(0, len(sentences), 2))


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
        self._clip_texts: dict[int, str] = {}
        self.interruptions = 0
        from assistant.voiceprint import VoicePrint

        self.voiceprint = VoicePrint()  # the model loads on first use
        self._capture_waiter: asyncio.Future | None = None
        # Words Jarvis is saying while the microphone already listens ("Да, сэр" after a bare wake word).
        self._echo_guard = ""
        self._await_until = 0.0

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
            from assistant.setup_models import ensure_vosk

            ensure_vosk(self.cfg.wake.model)  # first start of an installed build: models come on demand
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
        self._watch_devices()
        asyncio.create_task(self._check_updates())
        if self.pack is None and self.cfg.voice.pack != "none":
            asyncio.create_task(self._fetch_packs())
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
        if self.cfg.router.model and self.cfg.router.model != self.cfg.llm.local.model:
            await self.llm.local.warmup(self.cfg.router.model)
        if self.state is State.IDLE:
            self.ui.set_state(State.IDLE)

    async def _fetch_packs(self) -> None:
        """Recorded Jarvis reactions missing (a fresh installed build): download them in the background."""
        from assistant.voicepack import download_packs

        try:
            await asyncio.get_running_loop().run_in_executor(None, download_packs)
        except Exception as exc:
            log.warning("Реплики Джарвиса не скачались: %s", exc)
            return
        self.set_voice_pack(self.cfg.voice.pack)

    async def _check_updates(self) -> None:
        await asyncio.sleep(60)  # not during startup
        while self.cfg.ui.check_updates:
            from assistant import updates

            news = await updates.check()
            if news:
                self.ui.notify("Обновление Джарвиса", f"На GitHub {news}")
            await asyncio.sleep(6 * 3600)

    # ------------------------------------------------------------------ audio devices
    def _watch_devices(self) -> None:
        from assistant.audio.devices import DeviceWatcher

        self._devices = DeviceWatcher(lambda kinds: self._threadsafe(self._devices_changed, kinds))
        self._devices.start()
        asyncio.create_task(self._mic_watchdog())

    def _devices_changed(self, kinds: set[str]) -> None:
        asyncio.create_task(self._reopen_audio(kinds))

    async def _reopen_audio(self, kinds: set[str]) -> None:
        """Headphones plugged in / default device switched: reopen the microphone, speech and music outputs."""
        from assistant.audio.player import GATE

        def work() -> None:
            if self.mic is not None:
                self.mic.reopen(GATE.reinit)
            else:
                GATE.reinit()
            self.player.refresh_device()

        await asyncio.get_running_loop().run_in_executor(None, work)
        if "output" in kinds:
            await asyncio.get_running_loop().run_in_executor(None, self.music.reopen)
        log.info("Звук переключён на новые устройства")

    async def _mic_watchdog(self) -> None:
        """A removed USB microphone does not raise errors, it just goes quiet: reopen it."""
        while True:
            await asyncio.sleep(3)
            if self.mic is not None and self.mic.silent_for > 5:
                log.warning("Микрофон молчит %.0f с, переоткрываю", self.mic.silent_for)
                await self._reopen_audio({"input"})

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    async def shutdown(self) -> None:
        if getattr(self, "_devices", None) is not None:
            self._devices.stop()
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
        old_models = {self.cfg.llm.local.model, self.cfg.llm.local.vision_model, self.cfg.router.model}
        for key, value in changes.items():
            save_override(key, value)
        update_in_place(self.cfg, new)
        if {"llm.local.model", "llm.local.vision_model", "router.model"} & set(changes):
            # The old model would sit in video memory for keep_alive (30 min) next to the new one.
            kept = {self.cfg.llm.local.model, self.cfg.llm.local.vision_model, self.cfg.router.model}
            for model in old_models - kept:
                self.submit(self.llm.local.unload(model))
            self.submit(self.llm.local.warmup())
        self.tts.rate = self.cfg.tts.rate
        self.player.volume = self.cfg.tts.volume
        self.music.volume = self.cfg.music.volume
        self.music.duck_volume = self.cfg.music.duck_volume
        if "voice.pack" in changes:
            self.set_voice_pack(self.cfg.voice.pack)
        if "ui.autostart" in changes:
            from assistant import autostart

            autostart.set_enabled(bool(self.cfg.ui.autostart))
        if "privacy.log_phrases" in changes:
            from assistant.log import set_log_phrases

            set_log_phrases(self.cfg.privacy.log_phrases)
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
            spec = self.tts.set_voice(voice_id)
        except Exception:
            log.exception("Голос %s не загрузился", voice_id)
            self.ui.notify("Голос", f"Не удалось загрузить голос {voice_id}, остаётся прежний")
            return
        if spec.engine == "clone":
            self.tts.release("silero")  # it only spoke while the clone was loading

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
        await self.speaker.play_clip(clip, await self.clip_text(clip))
        return True

    async def clip_text(self, clip: AudioClip) -> str:
        """What a recorded clip says ("Да, сэр."): the microphone hears it too, so echo checks need the words."""
        key = id(clip)
        if key not in self._clip_texts and self.stt is not None:
            audio = resample(clip.samples, clip.sr, 16000)
            self._clip_texts[key] = await asyncio.get_running_loop().run_in_executor(None, self.stt.transcribe, audio)
        return self._clip_texts.get(key, "")

    # ------------------------------------------------------------------ control API (used by skills/UI)
    def interrupt(self) -> None:
        """Stop talking and drop the running request (except the caller itself)."""
        self.interruptions += 1  # a ringing alarm stops when the user says anything to Jarvis
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

    def _skill(self, name: str):
        return next((s for s in self.skills if s.name == name), None)

    def deck_paged(self, index: int) -> None:
        """The user flipped the card window by hand: the explanation speaks that card."""
        explain = self._skill("explain")
        if explain is not None:
            explain.user_paged(index)

    def deck_closed(self) -> None:
        """Esc / ✕ on the card window: stop talking about it."""
        explain = self._skill("explain")
        if explain is not None and explain.narration is not None:
            explain.user_closed()
        else:
            self.speaker.stop()

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
        log.info("Диктовка %.1f с за %.0f мс: %s", utt.audio.size / 16000, (time.perf_counter() - t) * 1000,
                 private(text[:120]))
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
            log.info("Стоп-слово «%s» не подтвердилось (слышно: «%s»), продолжаю", word, private(text[:80]))
            return
        log.info("Прерван словом «%s», слушаю команду", stops[0])
        self.interrupt()
        self._earcon(earcons.WAKE)
        if self.listener:
            self.listener.to_await(self.cfg.audio.await_command_sec)
        self._set_state(State.LISTENING, "слушаю новую команду")

    def _on_await_timeout(self) -> None:
        waiter, self._capture_waiter = self._capture_waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)
        if self.state in (State.LISTENING,):
            self._set_state(State.IDLE)

    async def record_utterance(self, timeout: float):
        """The next phrase as audio, not handled as a command (voice enrollment). None if nothing was said."""
        if not self.listener:
            return None
        self._capture_waiter = asyncio.get_running_loop().create_future()
        self._earcon(earcons.WAKE)
        self.listener.to_await(timeout, "capture")
        self._set_state(State.LISTENING, "запись фразы")
        try:
            return await asyncio.wait_for(asyncio.shield(self._capture_waiter), timeout + 16)
        except asyncio.TimeoutError:
            return None
        finally:
            self._capture_waiter = None

    def _on_utterance(self, utt: Utterance) -> None:
        if utt.source == "capture":
            waiter = self._capture_waiter
            if waiter is not None and not waiter.done():
                waiter.set_result(utt.audio)
            self._set_state(State.THINKING)
            return
        if utt.source == "dictation":
            asyncio.create_task(self._finish_dictation(utt))
            return
        self._task = asyncio.create_task(self._process_audio(utt))

    async def _process_audio(self, utt: Utterance) -> None:
        self._set_state(State.THINKING)
        t = time.perf_counter()
        res = await asyncio.get_running_loop().run_in_executor(None, self.stt.recognize, utt.audio)
        text = res.text
        log.info("Распознано за %.0f мс: «%s» (уверенность %.2f, речь %.1f с)", (time.perf_counter() - t) * 1000,
                 private(text), res.confidence, utt.voiced_sec)
        wake = self.cfg.wake
        if utt.source == "hot" and self._not_for_me(utt, res):
            self._set_state(State.IDLE)
            return
        if utt.source in ("wake", "hot") and not utt.barge_in and not await self._is_owner(utt.audio):
            self._set_state(State.IDLE)
            return
        if utt.source == "wake":
            if wake.verify_with_stt and text and not find_wake(text, wake.phrases, wake.verify_ratio):
                log.info("Ложное срабатывание кодового слова, игнорирую")
                self._set_state(State.IDLE)
                return
            if utt.barge_in:
                text = self._after_name(text)
        guard, self._echo_guard = self._echo_guard, ""
        if guard and utt.source == "await":
            text = strip_echo(text, guard)  # "Да, сэр. Сколько треков в музыке?" -> the question only
        # Outside a wake capture the name must be clear: "кто такой Дарвин" is not "Джарвис".
        query = strip_wake(text, wake.phrases, wake.verify_ratio if utt.source == "wake" else 90)
        if utt.source == "wake" and (len(query) < 2 or is_bare_wake(text, wake.phrases)):
            await self._bare_wake()
            return
        if len(query) < 2:
            if guard and time.monotonic() < self._await_until and self.listener:
                # It was only Jarvis's own "Да, сэр" in the microphone: keep waiting for the command.
                self.listener.to_await(self._await_until - time.monotonic())
                self._set_state(State.LISTENING)
            else:
                self._set_state(State.IDLE)
            return
        await self.handle_text(query, addressed=utt.source != "hot")

    async def _is_owner(self, audio) -> bool:
        """Owner-only mode: someone else's "Джарвис" (TV, guests) is ignored."""
        if not (self.cfg.wake.owner_only and self.voiceprint.enrolled):
            return True
        sim = await asyncio.get_running_loop().run_in_executor(None, self.voiceprint.similarity, audio)
        if sim is not None and sim < self.cfg.wake.owner_threshold:
            log.info("Чужой голос (сходство %.2f), игнорирую", sim)
            return False
        return True

    def _not_for_me(self, utt: Utterance, res) -> bool:
        """The hot window after an answer hears everyone: TV, people in the room, Jarvis's own echo."""
        reason = ""
        if utt.voiced_sec < 0.45 or not res.text:
            reason = "слишком коротко"
        elif res.confidence < 0.55:
            reason = f"неразборчиво ({res.confidence:.2f})"
        elif is_echo(res.text, self.speaker.last_text):
            reason = "эхо собственного ответа"
        if reason:
            log.info("Горячее окно: %s, игнорирую", reason)
        return bool(reason)

    def _after_name(self, text: str) -> str:
        """Barge-in: the recording may start with Jarvis's own words, the command follows the name."""
        span = find_wake(text, self.cfg.wake.phrases, self.cfg.wake.verify_ratio)
        if span is None:
            return text
        after = text[span[1]:].strip(" ,.!?")
        if len(after) >= 2:
            return text[span[0]:]
        own = set(normalize_command(self.speaker.last_text, strip_polite=False).split())
        before = [w for w in text[:span[0]].split() if normalize_command(w, strip_polite=False) not in own]
        return " ".join(before + [text[span[0]:span[1]]])

    async def _bare_wake(self) -> None:
        """Only "Джарвис" was said: answer "Да, сэр" and wait for the command. The microphone listens
        while the reply plays, so a question started over it is not lost; the reply is cut from it."""
        seconds = self.cfg.audio.await_command_sec
        self._await_until = time.monotonic() + seconds
        if self.listener:
            self.listener.to_await(seconds)
        self._set_state(State.LISTENING)
        clip = self.pack.pick("reply") if (self.pack and self.cfg.voice.reply_on_bare_wake) else None
        if clip is not None:
            self._echo_guard = await self.clip_text(clip)
            await self.speaker.play_clip(clip, self._echo_guard)
            if self.state is State.SPEAKING:
                self._set_state(State.LISTENING)

    # ------------------------------------------------------------------ main request path
    async def handle_text(self, query: str, addressed: bool = True) -> None:
        """Entry point for recognized speech and typed questions.
        addressed=False: heard in the hot window without the name, it may be meant for someone else."""
        if asyncio.current_task() is not self._task:
            self.interrupt()
            self._task = asyncio.current_task()
        self._set_state(State.THINKING, query)
        log.info("Запрос: %s", private(query))
        self.ui.chat_add("user", query)
        started = time.perf_counter()
        listen_after = True
        try:
            reply = await self.brain.handle(query, addressed=addressed)
            listen_after = await self._deliver(reply, started, query)
            while self.brain.extra:
                listen_after = await self._deliver(self.brain.extra.pop(0), started, query)
            while self.brain.deferred:
                skill, intent = self.brain.deferred.pop(0)
                listen_after = await self._deliver(await skill.handle(intent), started, query)
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
            self._after_answer(listen_after, expect_answer=self.brain.expects_answer())

    async def _deliver(self, reply: Reply, started: float, query: str = "") -> bool:
        if reply.spoken:
            return reply.listen_after
        text = ""
        shown = False

        def show_card(answer: str) -> None:
            """Long answers also go to the card window, as soon as the whole text is known."""
            nonlocal shown
            if not shown and answer and not answer.startswith("[") and len(answer) > self.cfg.ui.card_threshold_chars:
                shown = True
                title = (query[:1].upper() + query[1:]).rstrip("?.!")[:70] if query else "Ответ"
                self.ui.show_deck(Deck(title=title, cards=[Card("", answer_card_text(answer), "")], done=True))

        use_reaction = reply.reaction and (reply.reaction != "ok" or self.cfg.voice.ok_for_actions)
        if use_reaction and await self.react(reply.reaction):
            text = f"[{reply.reaction}] {reply.speech}".strip()
        elif reply.stream is not None:
            first = True
            parts: list[str] = []

            async def timed():
                nonlocal first
                async for delta in reply.stream:
                    if first:
                        first = False
                        log.info("Первый токен через %.2f с (%s)", time.perf_counter() - started,
                                 self.llm.last_provider or "-")
                    parts.append(delta)
                    yield delta
                show_card("".join(parts).strip())  # the model is done long before the voice is

            text = await self.speaker.speak(timed())
        elif reply.speech:
            text = await self.speaker.say(reply.speech)
        show_card(text)
        log.info("Ответ за %.2f с: %s", time.perf_counter() - started, private(text[:200]))
        if text:
            self.ui.chat_add("assistant", text)
        return reply.listen_after or reply.confirm is not None

    def _after_answer(self, listen_after: bool, expect_answer: bool = False) -> None:
        if self.listener and (listen_after or expect_answer) and self.mic and not self.mic.paused:
            # Hot window: follow-up without the wake word. Short delay skips the speaker echo tail.
            # After a question ("Закрыть Chrome или Яндекс Браузер?", "Удалить?") a short "да" must pass.
            source = "await" if expect_answer else "hot"

            def open_window() -> None:
                if self.listener and self.listener.mode is Mode.WAIT and self.state is not State.MUTED:
                    self.listener.to_await(self.cfg.assistant.hot_window_sec, source)
                    self._set_state(State.LISTENING, "можно без «Джарвис»")
            asyncio.get_running_loop().call_later(0.25, open_window)
        elif self.state is not State.MUTED:
            self._set_state(State.IDLE)
