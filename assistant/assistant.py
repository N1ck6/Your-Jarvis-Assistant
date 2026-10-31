"""Orchestrator: wires microphone, STT, brain, speech and UI together.

Threads: Qt main thread (UI), one asyncio loop thread (everything here),
the listener thread (VAD/wake) and PortAudio callback threads.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import time
from typing import Callable

from assistant import errors, skillpack
from assistant.audio import earcons
from assistant.audio.music import MusicPlayer
from assistant.audio.listener import ListenerCore, ListenerEvents, Microphone, Mode, Utterance
from assistant.audio.player import AudioClip, Player
from assistant.audio.vad import SileroVad
from assistant.audio.wake import VoskWake
from assistant.brain import Brain, Dialog
from assistant.config import Settings
from assistant.core import Card, Deck, State, UiPort, reading_time
from assistant.llm.hub import LlmHub
from assistant.log import private
from assistant.nlu import find_wake, is_bare_wake, is_echo, normalize_command, strip_echo, strip_wake
from assistant.skills.base import Reply, load_skills
from assistant.speech import Speaker
from assistant.stt import create_stt
from assistant.tts.manager import TtsManager
from assistant.voicepack import VoicePack, load_pack

log = logging.getLogger("assistant")
TALK_OVER_SOFT = 0.7   # "a little quieter": about -3 dB, still easy to follow
WAIT_PHRASES = ["Ищу, сэр.", "Минуту, сэр.", "Сейчас узнаю, сэр.", "Проверяю, сэр."]
BACKGROUND_GAP_SEC = 1.5   # a pause after the user's new request before an earlier answer is spoken
BACKGROUND_PREFIX = "По прошлому вопросу. "
FIRST_TOKEN_TIMEOUT_SEC = 45.0   # no answer at all by then (a hung provider): give up instead of "thinking" forever
# A new request that is about the running one: it is cancelled, not left to answer in the background.
_CANCELS = re.compile(r"^(стоп|хватит|отмена|отмени|не надо|забудь|нет|не то|замолчи|тихо)\b|я имел в виду|"
                      r"не (это|то) (я )?(спрашивал|просил|имел)|^(ты )?(меня )?не (понял|расслышал)")


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


_ASK_PREFIX = re.compile(
    r"^(?:(?:а|ну|слушай|скажи-ка|пожалуйста|будь добр)[,\s]+)*"
    r"(?:(?:покажи|расскажи|объясни|скажи|подскажи|напиши|выведи|опиши|нарисуй|построй|найди|дай)(?:\s+мне)?[,\s]+"
    r"(?:(?:про|о|об)\s+)?)?",
    re.I)


def card_title(query: str) -> str:
    """The card header: the subject, not the request as said ("покажи мне график x квадрат- 3 x" -> "График y = x² − 3x")."""
    if not query:
        return "Ответ"
    from assistant import visuals

    formula = visuals.spoken_formula(query) if re.search(r"график", query, re.I) else None
    if formula:
        return f"График y = {visuals.pretty(formula)}"
    text = _ASK_PREFIX.sub("", query.strip()).strip(" ,?.!") or query.strip(" ?.!")
    text = text[:1].upper() + text[1:]
    return text if len(text) <= 70 else text[:67].rsplit(" ", 1)[0] + "…"


# The answer's screen part starts here (brain.SCREEN_ADDON): "###" or a line "ЭКРАН:".
_NOTES = re.compile(r"#{3,}|^[ \t]*(?:ЭКРАН|Экран|НА ЭКРАНЕ)\s*:", re.M)
_NOTES_PREFIXES = ("ЭКРАН:", "НА ЭКРАНЕ:")


def _maybe_marker_start(tail: str) -> bool:
    """The end of the text so far could be the beginning of the notes marker: hold it back from the voice."""
    if tail.endswith("#"):
        return True
    line = tail.rsplit("\n", 1)[-1].strip().upper()
    return bool(line) and any(p.startswith(line) for p in _NOTES_PREFIXES)


async def split_notes(stream, notes: list[str]):
    """Yields the spoken part of an answer stream; the screen part after the marker goes to `notes`."""
    buf = ""
    async for delta in stream:
        if notes:
            notes.append(delta)
            continue
        buf += delta
        m = _NOTES.search(buf)
        if m:
            if buf[:m.start()].strip():
                yield buf[:m.start()]
            notes.append(buf[m.end():])
            buf = ""
            continue
        if _maybe_marker_start(buf):
            cut = len(buf.rstrip("#")) if buf.endswith("#") else buf.rfind("\n") + 1
            if cut > 0:
                yield buf[:cut]
                buf = buf[cut:]
            continue
        yield buf
        buf = ""
    if buf and not notes:
        yield buf


class Assistant:
    def __new__(cls, *args, **kwargs):
        self = super().__new__(cls)
        # Requests still waiting for their answer to start (a slow search): a new request does not cancel them, they
        # go on in the background and speak once the user has finished the new one (_my_turn).
        self._waiting: set[asyncio.Task] = set()
        self._background: set[asyncio.Task] = set()
        return self

    def __init__(self, cfg: Settings, ui: UiPort, *, use_mic: bool = True) -> None:
        self.cfg = cfg
        self.ui = ui
        self.use_mic = use_mic
        self.loop: asyncio.AbstractEventLoop | None = None
        self.state = State.IDLE
        self._state_at = time.monotonic()
        # Called on every state change in the loop thread (the browser extension's icon follows it).
        self.state_listeners: list[Callable[[State], None]] = []
        self.voicelab_url = f"http://{cfg.voicelab.host}:{cfg.voicelab.port}/"
        self.on_exit: Callable[[], None] = lambda: None
        # Started with Windows: no greeting and nothing spoken at startup (missed reminders go to the tray).
        self.quiet_start = False
        self.updated = False     # restarted by itself on new code (logged)

        self.dialog = Dialog(cfg.assistant.dialog_ttl_sec, cfg.assistant.dialog_max_turns)
        self.llm = LlmHub(cfg.llm)
        self.llm.on_unavailable = lambda name, why: self.ui.notify("Облачная модель недоступна", why)
        self.tts = TtsManager(cfg.tts.voice, cfg.tts.rate, clone_nfe=cfg.tts.clone_nfe)
        self.pack: VoicePack | None = load_pack(cfg.voice.pack)
        self.player = Player(cfg.audio.output_device, cfg.tts.volume)
        self.music = MusicPlayer(cfg.music.volume, cfg.music.duck_volume)
        from assistant.audio.media_session import MediaSession

        self.media_session = MediaSession(self.music)  # the Windows mini player and media keys control the music
        self.speaker = Speaker(self.tts, self.player, on_start=self._on_speech_start, on_end=self._on_speech_end)
        self.skills = load_skills(cfg.skills.enabled, cfg.skills.disabled)
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
        self._dictating = False
        self._dict_chain: asyncio.Task | None = None   # parts are recognized and pasted strictly in order
        self._dict_pasted = 0                          # parts pasted in this dictation (a space before the next)
        self._dict_key_stops = False                   # this hotkey press stops a running dictation
        # Pasting presses Ctrl+V: it must wait while the user still holds Ctrl/Alt of the hotkey.
        self.keys_held: Callable[[], bool] = lambda: False
        self._clip_texts: dict[int, str] = {}
        self.interruptions = 0
        from assistant.voiceprint import VoicePrint

        self.voiceprint = VoicePrint()  # the model loads on first use
        self._capture_waiter: asyncio.Future | None = None
        # Words Jarvis is saying while the microphone already listens ("Да, сэр" after a bare wake word).
        self._echo_guard = ""
        self._await_until = 0.0
        # The request that was running when a wake word was heard over it: it is cancelled only once the recognizer
        # confirms the name (a noise, the TV or Jarvis's own echo often sounds like "Джарвис" to the spotter).
        self._held: asyncio.Task | None = None
        self._held_at = 0.0

    # ------------------------------------------------------------------ startup
    def load_models(self) -> None:
        """Slow part (seconds): run before the loop starts (or while the previous Jarvis still works: handover)."""
        if self.stt is not None:
            return   # loaded already, before the handover
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
                on_talk_over=self._on_talk_over,
            )
            self.listener = ListenerCore(
                SileroVad(self.cfg.audio.sample_rate),
                VoskWake(self.cfg.resolve(wake_cfg.model), wake_cfg.phrases, wake_cfg.decoys, wake_cfg.stop_words),
                events, sr=self.cfg.audio.sample_rate, vad_threshold=self.cfg.audio.vad_threshold,
                end_silence_ms=self.cfg.audio.end_silence_ms, max_utterance_sec=self.cfg.audio.max_utterance_sec,
                wake_grace_ms=self.cfg.audio.wake_grace_ms,
                pre_roll_ms=self.cfg.audio.pre_roll_ms)
            aec = None
            if self.cfg.audio.echo_cancellation:
                from assistant.audio import aec as aec_mod

                aec = aec_mod.create()
                if aec is not None:
                    log.info("Эхоподавление включено: Джарвис не слышит сам себя")
            self.mic = Microphone(self.listener, self.cfg.audio.input_device, self.cfg.audio.sample_rate, aec)
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
        asyncio.create_task(self._watch_skills())
        asyncio.create_task(self._check_updates())
        asyncio.create_task(self._watch_code())
        asyncio.create_task(self._watch_thinking())
        if self.pack is None and self.cfg.voice.pack != "none":
            asyncio.create_task(self._fetch_packs())
        self._set_state(State.IDLE)
        self._ready.set()
        log.info("Готов. Скажите «%s» и команду.", self.cfg.assistant.name)
        if self.updated:
            from assistant import selfupdate
            from assistant.paths import ROOT

            log.info("Обновлён: %s", await asyncio.to_thread(selfupdate.subject, ROOT))
        if self.cfg.voice.greet_on_start and self.pack and not self.quiet_start:
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
        if not await self.llm.local.alive():
            from assistant.llm import ollama_keeper

            if await asyncio.to_thread(ollama_keeper.start_server):
                log.info("Запускаю Ollama")
                for _ in range(30):
                    await asyncio.sleep(0.5)
                    if await self.llm.local.alive():
                        break
        await self.llm.local.warmup()
        if self.llm.local.downgraded:
            big, small = self.llm.local.downgraded
            self.ui.notify("Локальная модель", f"{big} не помещается в память — работаю на {small}")
        if self.cfg.router.model and self.cfg.router.model != self.cfg.llm.local.model:
            await self.llm.local.warmup(self.cfg.router.model)
        if self.state is State.IDLE:
            self.ui.set_state(State.IDLE)
        asyncio.create_task(self._watch_local())

    async def _watch_local(self) -> None:
        """Ollama closed or restarted: the router and answers switch to the cloud and back by themselves; Ollama is
        started if it is installed. A model pushed partly onto the CPU (another program held the video memory when
        it loaded) is reloaded whole once the memory is free and Jarvis is idle (see ollama_keeper)."""
        from assistant.llm import ollama_keeper as keeper

        local = self.llm.local
        started_at = time.monotonic()
        last_reload, told_slow = 0.0, False
        while True:
            await asyncio.sleep(30)
            if not await local.alive():
                if local.healthy:
                    local.healthy = False
                    log.warning("Ollama не отвечает: маршрутизатор и ответы идут через облако, пока она не вернётся")
                if time.monotonic() - started_at > 300 and await asyncio.to_thread(keeper.start_server):
                    started_at = time.monotonic()
                    log.info("Запускаю Ollama")
                continue
            if not local.healthy:
                if await local.warmup():
                    log.info("Ollama снова доступна: локальная модель вернулась")
                continue
            place = await local.placement()
            if place is None or not place[0]:
                continue   # unloaded after keep_alive: it is placed again on the next request
            size, vram = place
            if vram / size >= keeper.WHOLE:
                told_slow = False
                continue
            if not told_slow:
                told_slow = True
                log.warning("Локальная модель на %d%% на процессоре: видеокарта занята другой программой, "
                            "ответы медленнее; перезагружу её, когда память освободится", round(100 - 100 * vram / size))
            if self.idle_for() < 10 or time.monotonic() - last_reload < 120:
                continue
            free = await asyncio.to_thread(keeper.gpu_free_mb)
            if keeper.worth_reload(size, vram, free):
                last_reload = time.monotonic()
                await local.unload(local.cfg.model)
                await local.warmup()
                place = await local.placement()
                if place and place[0]:
                    log.info("Локальная модель перезагружена: %d%% на видеокарте", round(100 * place[1] / place[0]))

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

    async def _watch_thinking(self) -> None:
        """Safety net: "thinking" (the orange icon) with nothing left working on a request goes back to idle."""
        while True:
            await asyncio.sleep(5)
            working = (self._task is not None and not self._task.done()) or any(not t.done() for t in self._background)
            if self.state is State.THINKING and not working and time.monotonic() - self._state_at > 10:
                log.warning("Значок «думаю» остался без запроса: возвращаю в ожидание")
                self._set_state(State.IDLE)

    def idle_for(self) -> float:
        """Seconds Jarvis has been doing nothing (0 while he listens, thinks, speaks, dictates or plays music)."""
        busy = (self.state not in (State.IDLE, State.MUTED) or self.speaker.speaking or self._dictating
                or (self._task is not None and not self._task.done())
                or getattr(self.music, "state", "stopped") == "playing")
        return 0.0 if busy else time.monotonic() - self._state_at

    async def _watch_code(self) -> None:
        """A new commit in Jarvis's folder (an edit, a git pull): checked at once, while he works; then a handover
        to the new code once he has been idle a while (see selfupdate)."""
        from assistant import selfupdate
        from assistant.paths import ROOT

        seen = await asyncio.to_thread(selfupdate.head, ROOT)
        if seen is None:
            return   # not a git checkout (an installed build)
        failed = None
        while True:
            await asyncio.sleep(5)
            now = await asyncio.to_thread(selfupdate.head, ROOT)
            if not self.cfg.ui.restart_on_update or now in (None, seen, failed):
                continue
            log.info("Новый код (%s): проверяю, пока работаю", now[:7])
            error = await asyncio.to_thread(selfupdate.check, ROOT)
            if await asyncio.to_thread(selfupdate.head, ROOT) != now:
                continue     # another commit came during the check: check that one
            if error:
                failed = now
                log.error("Новый код не запускается, остаюсь на прежнем: %s", error)
                self.ui.notify("Джарвис не обновился", f"В новом коде ошибка: {error[:180]}")
                continue
            log.info("Код проверен: перезапущусь, когда буду свободен")
            while self.idle_for() < 15:
                await asyncio.sleep(1)
            if await asyncio.to_thread(selfupdate.head, ROOT) != now:
                continue
            if await self._handover(quiet=True, updated=True):
                return
            failed = now

    async def _handover(self, quiet: bool, updated: bool) -> bool:
        """Starts the new Jarvis next to this one: it loads its models while this one keeps answering, and takes
        the microphone, tray and hotkeys the moment this one exits: about a second without Jarvis instead of ~10.
        False if it did not come up (this one stays)."""
        from assistant import selfupdate
        from assistant.paths import ROOT

        if getattr(self, "_handing_over", False):
            return False
        self._handing_over = True
        try:
            selfupdate.clear()
            proc = await asyncio.to_thread(selfupdate.spawn, ROOT, quiet, updated)
            log.info("Новый Джарвис загружается рядом (pid %d)", proc.pid)
            deadline = time.monotonic() + 180
            while not selfupdate.is_ready(proc.pid):
                if proc.poll() is not None or time.monotonic() > deadline:
                    if proc.poll() is None:
                        selfupdate.kill(proc)
                    log.error("Новый Джарвис не запустился, остаюсь")
                    self.ui.notify("Джарвис не перезапустился", "Новый экземпляр не загрузился: см. data/logs")
                    return False
                await asyncio.sleep(0.3)
            while self.idle_for() < 1.5:     # the user is talking to this one: finish first
                if proc.poll() is not None:
                    return False
                await asyncio.sleep(0.2)
            log.info("Передаю работу новому Джарвису")
            self.on_exit()
            return True
        finally:
            self._handing_over = False

    # ------------------------------------------------------------------ skills
    async def reload_skills(self) -> list[str]:
        """Skills turned on or off, added to data/skills/ or edited there: applied without a restart.
        Unchanged skills keep running (their timers, playing music). Returns the ids of the loaded skills."""
        from assistant.skills.base import create_skill, plan_skills, warn_missing

        current = {s.name: s for s in self.skills}
        new: list = []
        for name, m in plan_skills(self.cfg.skills.enabled, self.cfg.skills.disabled):
            old = current.get(name)
            changed = old is not None and m is not None and not m.builtin and skillpack.signature(m.path) != old.files
            if old is not None and not changed:
                new.append(old)
                continue
            try:
                skill = create_skill(name, m, fresh=changed)
                skill.setup(self)
                await skill.start()
            except Exception:
                log.exception("Навык %s не загружен", name)
                if old is not None:
                    new.append(old)  # the edited version is broken: the old one keeps working
                continue
            if old is not None:
                await self._stop_skill(old)
            log.info("Навык %s %s", name, "обновлён" if old is not None else "загружен")
            new.append(skill)
        kept = {id(s) for s in new}
        for old in current.values():
            if id(old) not in kept:
                await self._stop_skill(old)
                log.info("Навык %s выключен", old.name)
        self.skills[:] = new  # the brain holds the same list
        warn_missing(self.skills)
        self.brain.rebuild_tools()
        self.brain.router.reset_catalog()
        if self.brain.last_command is not None and id(self.brain.last_command[0]) not in kept:
            self.brain.last_command = None
        return [s.name for s in self.skills]

    async def _stop_skill(self, skill) -> None:
        try:
            await skill.stop()
        except Exception:
            log.exception("stop() %s", skill.name)

    async def _watch_skills(self) -> None:
        """Your own skills folder (data/skills/): a new, removed or edited skill is picked up in a few seconds."""
        last = skillpack.signature(skillpack.USER_DIR)
        while True:
            await asyncio.sleep(3)
            now = await asyncio.to_thread(skillpack.signature, skillpack.USER_DIR)
            if now != last:
                last = now
                await asyncio.sleep(1)  # a copy in progress: let it finish
                last = await asyncio.to_thread(skillpack.signature, skillpack.USER_DIR)
                log.info("Папка навыков изменилась, перечитываю")
                await self.reload_skills()

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
            await self._stop_skill(skill)

    # ------------------------------------------------------------------ helpers
    def _threadsafe(self, func, *args) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(func, *args)

    def submit(self, coro) -> None:
        """Run a coroutine on the assistant loop from any thread (UI buttons, hotkeys)."""
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _set_state(self, state: State, detail: str = "") -> None:
        mic = getattr(self, "mic", None)
        if state is State.IDLE and mic is not None and mic.paused and not getattr(self, "_dictating", False):
            # "выключи микрофон" is answered aloud, and the end of that answer must not show "Жду «Джарвис»":
            # the tray icon and its menu item would then say the opposite of the truth.
            state = State.MUTED
        self.state = state
        self._state_at = time.monotonic()
        # Music steps aside while Jarvis listens, thinks or talks.
        self.music.duck(state in (State.LISTENING, State.THINKING, State.SPEAKING))
        self.ui.set_state(state, detail)
        for listener in self.state_listeners:
            listener(state)

    def _earcon(self, clip: AudioClip) -> None:
        if self.cfg.audio.earcons:
            self.player.effect(clip, self.cfg.audio.earcon_volume)

    def _on_speech_start(self) -> None:
        if self.listener:
            # Talk this long without "Джарвис" is a conversation, not a call (the pause waits longer for it).
            self.listener.talk_over_limit = 15.0 if self.cfg.audio.talk_over == "pause" else 2.0
            self.listener.assistant_speaking = True
        self._set_state(State.SPEAKING)

    def _on_talk_over(self, talking: bool) -> None:
        """Someone talks over Jarvis (listener thread): a little quieter, much quieter or a pause (the setting),
        so the rest of the phrase, maybe "Джарвис, …", is heard over less echo; back when the talk is over."""
        if self._held is not None:
            return  # a wake word over the answer is being checked: the voice stays down until it is
        if not talking:
            self.player.release()
            return
        aec = self.mic.aec if self.mic else None
        if aec is None or not aec.adapted:
            return  # the first seconds the canceller lets echo through: that is not the user
        mode = self.cfg.audio.talk_over
        if mode == "pause":
            self.player.hold = True
        elif mode in ("soft", "strong"):
            self.player.duck = TALK_OVER_SOFT if mode == "soft" else max(0.0, min(1.0, self.cfg.audio.talk_over_volume))

    def _talk_over_again(self) -> None:
        """The wake word check is over: back to what the talk in the room asks for now."""
        self.player.release()
        if self.listener and self.listener.talking_over:
            self._on_talk_over(True)

    def _on_speech_end(self) -> None:
        if self.listener:
            self.listener.assistant_speaking = False
        self.player.release()

    def apply_settings(self, changes: dict) -> list[str]:
        """Saves changes, updates the live settings; returns the keys that need a restart."""
        from assistant import skillpack
        from assistant.config import save_override, update_in_place, validate_changes
        from assistant.settings_schema import RESTART_KEYS

        ours, changes = skillpack.split_changes(changes)  # settings of folder skills ("focus.work_min")
        new = validate_changes(changes)  # raises on bad values, nothing is saved then
        restart = skillpack.save_changes(ours)
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
        log.info("Настройки изменены: %s", ", ".join([*changes, *(c[1] for c in ours)]))
        return restart + [k for k in changes if k in RESTART_KEYS]

    def _switch_voice(self, voice_id: str) -> None:
        try:
            spec = self.tts.set_voice(voice_id)
        except Exception:
            log.exception("Голос %s не загрузился", voice_id)
            self.ui.notify("Голос", f"Не удалось загрузить голос {voice_id}, остаётся прежний")
            return
        if spec.engine == "clone":
            self.tts.release("silero")  # it only spoke while the clone was loading

    def restart(self, quiet: bool = False, updated: bool = False) -> None:
        """A fresh instance takes over (settings that need a restart): loaded next to this one, see _handover."""
        if self.loop is not None and self.loop.is_running():
            self.submit(self._restart_soon(quiet, updated))
        else:
            self._restart_cold(quiet, updated)

    async def _restart_soon(self, quiet: bool, updated: bool) -> None:
        if not await self._handover(quiet, updated):
            self._restart_cold(quiet, updated)

    def _restart_cold(self, quiet: bool = False, updated: bool = False) -> None:
        """Starts a fresh instance (it waits for this one to release the lock) and exits. quiet: no greeting."""
        import subprocess
        import sys

        from assistant.paths import ROOT

        exe = ROOT / ".venv" / "Scripts" / "pythonw.exe"
        cmd = [str(exe) if exe.exists() else sys.executable, "-m", "assistant", "--wait-lock"]
        cmd += ["--quiet"] * quiet + ["--updated"] * updated
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
    def interrupt(self, background: bool = True) -> None:
        """Stop talking and drop the running request (except the caller itself); background=True: the requests
        left answering in the background too ("стоп", the button, the microphone off)."""
        self.interruptions += 1  # a ringing alarm stops when the user says anything to Jarvis
        self._held = None
        self.speaker.stop()
        tasks = [self._task, *(self._background if background else ())]
        for task in tasks:
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()

    def _to_background(self, task: asyncio.Task) -> None:
        """The request is still waiting for its answer: it goes on while the user asks the next one."""
        log.info("Прошлый запрос ещё ищет ответ: продолжаю его в фоне")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        if self._held is task:
            self._held = None
            self._talk_over_again()

    def _can_wait(self, task: asyncio.Task | None) -> bool:
        return task is not None and not task.done() and task in self._waiting

    async def _my_turn(self) -> None:
        """An answer from the background speaks once the user has finished the new request (and Jarvis is not
        speaking another answer), after a short pause to take a breath."""
        quiet_since = None
        while True:
            busy = (self.state is State.LISTENING or self.speaker.speaking or self._dictating
                    or (self.listener is not None and self.listener.mode is Mode.CAPTURE))
            now = time.monotonic()
            if busy:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= BACKGROUND_GAP_SEC:
                return
            await asyncio.sleep(0.1)

    async def _first_token(self, stream, started: float):
        """Waits for the answer to start; says "Ищу, сэр" if that takes a while. None if the answer is empty."""
        task = asyncio.current_task()
        self._waiting.add(task)
        filler = asyncio.create_task(self._wait_phrase(task, started))
        try:
            first = await asyncio.wait_for(stream.__anext__(), FIRST_TOKEN_TIMEOUT_SEC)
        except StopAsyncIteration:
            return None
        except asyncio.TimeoutError:
            log.warning("Ответ не начался за %.0f с: бросаю ожидание", FIRST_TOKEN_TIMEOUT_SEC)
            self.ui.notify("Джарвис: не дождался ответа", "Модель не ответила вовремя — спросите ещё раз")
            return None
        finally:
            self._waiting.discard(task)
            if not filler.done() and not getattr(filler, "speaking", False):
                filler.cancel()
        log.info("Первый токен через %.2f с (%s)", time.perf_counter() - started, self.llm.last_provider or "-")
        return first

    async def _wait_phrase(self, task: asyncio.Task, started: float) -> None:
        """Counted from the request itself (recognition and routing take a second or two of it)."""
        delay = self.cfg.voice.wait_phrase_sec
        if delay <= 0:
            return
        await asyncio.sleep(max(0.3, delay - (time.perf_counter() - started)))
        if (task is self._task and self.state is State.THINKING and not self.speaker.speaking
                and not (self.listener is not None and self.listener.mode is Mode.CAPTURE)):
            asyncio.current_task().speaking = True   # type: ignore[attr-defined]  # let it finish the phrase
            await self.speaker.say(random.choice(WAIT_PHRASES))
            if task is self._task and not task.done():
                self._set_state(State.THINKING)

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

    def _cancel_if_busy(self) -> bool:
        """The button pressed again, or by accident, or someone else said "Джарвис": while Jarvis listens or works
        on the request, the button switches him off instead of starting over. Not while he talks: then the button
        is how the user cuts in with a new question."""
        if self._dictating or self.state not in (State.LISTENING, State.THINKING):
            return False
        log.info("Кнопка Джарвиса: слушание отменено")
        self.interrupt()
        self._echo_guard, self._await_until = "", 0.0
        if self.listener:
            self.listener.to_wait()
        self._earcon(earcons.CANCEL)
        self._set_state(State.IDLE)
        return True

    def listen_now(self) -> None:
        """Hotkey / tray: start listening without the wake word; pressed while listening, stop."""
        if not self.listener or self._cancel_if_busy():
            return
        if self.mic and self.mic.paused:
            self.set_muted(False)
        self.interrupt()
        self._earcon(earcons.WAKE)
        self.listener.to_await(self.cfg.audio.await_command_sec)
        self._set_state(State.LISTENING)

    def wake_up(self) -> None:
        """The same as saying "Джарвис" alone (the browser extension's button): "Да, сэр" and wait for the command.
        Pressed while Jarvis listens or thinks: he stops (an accidental call costs no request)."""
        if not self.listener or self._cancel_if_busy():
            return
        if self.mic and self.mic.paused:
            self.set_muted(False)
        self.interrupt()
        self._earcon(earcons.WAKE)
        self.submit(self._bare_wake())

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

    async def ask_yes_no(self, question: str, timeout: float = 45.0) -> bool | None:
        """A question from background work (the browser agent): spoken, then "да" / "нет" is heard without the name
        (or later with it). None: no answer in time, or the user went on to something else."""
        answer: asyncio.Future = asyncio.get_running_loop().create_future()

        async def yes() -> Reply:
            if not answer.done():
                answer.set_result(True)
            return Reply("Выполняю.", listen_after=False)

        async def no() -> Reply:
            if not answer.done():
                answer.set_result(False)
            return Reply("Хорошо, не буду.", listen_after=False)

        self.brain.pending = (yes, time.monotonic() + timeout, no)
        await self.announce(question)
        self._after_answer(False, expect_answer=True)
        try:
            return await asyncio.wait_for(asyncio.shield(answer), timeout)
        except asyncio.TimeoutError:
            if self.brain.pending is not None and self.brain.pending[0] is yes:
                self.brain.pending = None
            return None

    # ------------------------------------------------------------------ dictation
    def start_dictation(self, until_pause: bool, paste: Callable[[str], None], pause_sec: float | None = None) -> bool:
        """Records speech and types it where the cursor is, phrase by phrase while the user speaks.
        A start cue plays first; recording begins right after it. Hold-hotkey mode ends with stop_dictation();
        until_pause: hands-free, ends after pause_sec without speech (default: [audio] dictation_pause_sec)."""
        if not self.listener or not self.mic or self._dictating:
            return False
        self._muted_before_dictation = self.mic.paused
        if self.mic.paused:
            self.mic.resume()
        self.interrupt()
        self._dictating = True
        self._dictation_paste = paste
        self._dict_pasted = 0
        self._dict_chain = None
        cue = earcons.DICTATE
        self.player.effect(cue, max(self.cfg.audio.earcon_volume, 0.5))  # always: the user waits for it

        def begin() -> None:
            if self._dictating and self.listener:  # not released / cancelled during the cue
                self.listener.start_dictation(until_pause, pause_sec or self.cfg.audio.dictation_pause_sec)
                self._set_state(State.LISTENING, "диктовка")
        asyncio.get_running_loop().call_later(cue.seconds + 0.04, begin)
        self._set_state(State.LISTENING, "диктовка")
        return True

    def stop_dictation(self) -> None:
        if self.listener and self.listener.dictating:
            self.listener.stop_dictation()
        elif self._dictating and self._dict_chain is None:
            self._end_dictation()  # released during the start cue: nothing was recorded

    def dictation_key_down(self, paste: Callable[[str], None]) -> None:
        """Hotkey pressed: starts dictation, or stops one that is running (tap mode)."""
        self._dict_key_stops = self._dictating
        if self._dictating:
            self.stop_dictation()
        else:
            self.start_dictation(until_pause=False, paste=paste)

    def dictation_key_up(self, held_sec: float) -> None:
        """Held: release ends the dictation. Tapped: it goes on hands-free with text appearing as you speak,
        until the next tap or [audio] dictation_hands_free_sec without speech after the last word."""
        if self._dict_key_stops or not self._dictating:
            return
        if held_sec < 0.45:
            pause = self.cfg.audio.dictation_hands_free_sec
            if self.listener:
                self.listener.end_dictation_after_pause(pause)
            log.info("Диктовка без удержания: до повторного нажатия или %.0f с без речи", pause)
            return
        self.stop_dictation()

    def _on_dictation(self, utt: Utterance, last: bool) -> None:
        """A phrase is ready: recognize it and paste it after the previous one, while the user keeps speaking."""
        prev = self._dict_chain
        self._dict_chain = asyncio.create_task(self._dictation_step(prev, utt, last))

    async def _dictation_step(self, prev: asyncio.Task | None, utt: Utterance, last: bool) -> None:
        if prev is not None:
            try:
                await prev
            except Exception:
                log.exception("Диктовка: часть не вставлена")
        loop = asyncio.get_running_loop()
        paste = self._dictation_paste
        if utt.audio.size and paste is not None:
            t = time.perf_counter()
            res = await loop.run_in_executor(None, self.stt.recognize, utt.audio)
            text = res.text.strip()
            log.debug("Диктовка: фраза %.1f с за %.0f мс (уверенность %.2f): %s", utt.audio.size / 16000,
                     (time.perf_counter() - t) * 1000, res.confidence, private(text[:120]))
            if text and (res.confidence >= 0.45 or utt.voiced_sec >= 1.0):  # a cough or a click is not a phrase
                piece = (" " if self._dict_pasted else "") + text
                await loop.run_in_executor(None, self._paste_when_keys_free, paste, piece)
                self._dict_pasted += 1
        if last:
            if self._dict_pasted:
                self._earcon(earcons.DONE)
            self._end_dictation()

    def _paste_when_keys_free(self, paste: Callable[[str], None], text: str) -> None:
        """While the hotkey is held, Ctrl+V would become Ctrl+Alt+V: the phrase waits for the release."""
        deadline = time.monotonic() + 600
        while self.keys_held() and time.monotonic() < deadline:
            time.sleep(0.05)
        paste(text)

    def _end_dictation(self) -> None:
        self._dictating = False
        self._dictation_paste = None
        self._dict_chain = None
        if self._muted_before_dictation and self.mic:
            self.mic.pause()
        self._set_state(State.MUTED if self._muted_before_dictation else State.IDLE)

    # ------------------------------------------------------------------ listener events
    def _on_wake(self) -> None:
        # Jarvis saying his own name through the speakers is not a wake word.
        if self.speaker.speaking and find_wake(self.speaker.recent_text(), self.cfg.wake.phrases, 85):
            log.info("Своё имя в собственной речи, игнорирую")
            if self.listener:
                self.listener.to_wait()
            return
        task = self._task
        if task is not None and not task.done():
            # Not cancelled yet: the request goes on (quietly) unless the recognizer confirms the name.
            log.info("Кодовое слово во время запроса: проверяю, прежде чем отменить")
            self._held, self._held_at = task, time.monotonic()
            self.player.duck = min(self.player.duck, 0.15)
        else:
            self.interrupt(background=False)   # answers still searching in the background go on
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
            log.debug("Стоп-слово «%s» не подтвердилось (слышно: «%s»), продолжаю", word, private(text[:80]))
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
        if utt.source in ("dictation_part", "dictation"):
            self._on_dictation(utt, last=utt.source == "dictation")
            return
        if self._held is not None and utt.source == "wake":
            asyncio.create_task(self._hear_over(utt, self._held))
            return
        self._task = asyncio.create_task(self._process_audio(utt))

    async def _hear_over(self, utt: Utterance, held: asyncio.Task) -> None:
        """A phrase that began with a wake word while a request was running: a new request cancels that one
        (handle_text); a false wake word lets it go on as if nothing happened."""
        try:
            await self._process_audio(utt)
        finally:
            if self._held is held:
                self._held = None
                self._talk_over_again()
                if not held.done():
                    log.info("Имя не подтвердилось: продолжаю прежний запрос")
                    self._set_state(State.SPEAKING if self.speaker.speaking else State.THINKING)

    def _drop_held(self) -> None:
        """The name was confirmed (a bare "Джарвис"): the request it was said over is dropped, unless it is still
        waiting for its answer: then it goes on in the background (the user is about to ask something else)."""
        if self._held is not None:
            if self._can_wait(self._held):
                self._to_background(self._held)
            else:
                self.interrupt(background=False)

    async def _wait_hold(self) -> None:
        """A wake word heard over this request is being checked: its answer waits, it may be cancelled."""
        while self._held is not None and self._held is asyncio.current_task():
            if time.monotonic() - self._held_at > 20:
                self._held = None   # never resolved (a lost utterance): go on
                self._talk_over_again()
                break
            await asyncio.sleep(0.05)

    async def _process_audio(self, utt: Utterance) -> None:
        self._set_state(State.THINKING)
        t = time.perf_counter()
        res = await asyncio.get_running_loop().run_in_executor(None, self.stt.recognize, utt.audio)
        text = res.text
        log.info("Распознано за %.0f мс: «%s» (уверенность %.2f, речь %.1f с)", (time.perf_counter() - t) * 1000,
                 private(text), res.confidence, utt.voiced_sec)
        wake = self.cfg.wake
        if utt.source in ("hot", "await") and self._only_name(text):
            # "Джарвис" again after an answer (to ask something else): the window starts over, nothing is lost.
            self._rearm_window()
            return
        if utt.source == "hot" and self._not_for_me(utt, res):
            self._set_state(State.IDLE)
            return
        if utt.source in ("wake", "hot") and not utt.barge_in and not await self._is_owner(utt.audio):
            self._set_state(State.IDLE)
            return
        if utt.source == "wake":
            if wake.verify_with_stt and text and not find_wake(text, wake.phrases, wake.verify_ratio):
                if not self._request_without_name(utt, res):
                    log.info("Ложное срабатывание кодового слова, игнорирую")
                    self._set_state(State.IDLE)
                    return
                log.info("Имени в записи не слышно, но это ясный запрос: выполняю")
            if utt.barge_in:
                text = self._after_name(text)
        guard, self._echo_guard = self._echo_guard, ""
        if guard and utt.source == "await":
            text = strip_echo(text, guard)  # "Да, сэр. Сколько треков в музыке?" -> the question only
        # Outside a wake capture the name must be clear: "кто такой Дарвин" is not "Джарвис".
        query = strip_wake(text, wake.phrases, wake.verify_ratio if utt.source == "wake" else 90)
        if utt.source == "wake" and (len(query) < 2 or is_bare_wake(text, wake.phrases)):
            self._drop_held()
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

    def _only_name(self, text: str) -> bool:
        phrases = self.cfg.wake.phrases
        return bool(text) and find_wake(text, phrases, 90) is not None and len(strip_wake(text, phrases, 90)) < 2

    def _rearm_window(self) -> None:
        if not self.listener:
            return
        seconds = max(self.cfg.assistant.hot_window_sec, self.cfg.audio.await_command_sec)
        self._earcon(earcons.WAKE)
        self.listener.to_await(seconds, "await", resume=True)
        self._set_state(State.LISTENING, "слушаю")
        log.info("Имя в горячем окне: слушаю заново %.0f с", seconds)

    async def _is_owner(self, audio) -> bool:
        """Owner-only mode: someone else's "Джарвис" (TV, guests) is ignored."""
        if not (self.cfg.wake.owner_only and self.voiceprint.enrolled):
            return True
        sim = await asyncio.get_running_loop().run_in_executor(None, self.voiceprint.similarity, audio)
        if sim is not None and sim < self.cfg.wake.owner_threshold:
            log.info("Чужой голос (сходство %.2f), игнорирую", sim)
            return False
        return True

    @staticmethod
    def _request_without_name(utt: Utterance, res) -> bool:
        """The spotter heard the name but the recognizer lost it ("Какого персонажа из Dota 2 называют…"): a long,
        clear question or command still goes through. Noise and the TV are rarely all of that at once."""
        from assistant.brain import is_info_question
        from assistant.nlu import looks_like_command

        norm = normalize_command(res.text)
        return (not utt.barge_in and res.confidence >= 0.8 and utt.voiced_sec >= 1.2 and len(norm.split()) >= 3
                and (looks_like_command(norm) or is_info_question(res.text) or res.text.rstrip().endswith("?")))

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
            if self._can_wait(self._task) and not _CANCELS.search(normalize_command(query, strip_polite=False)):
                self._to_background(self._task)
            else:
                self.interrupt(background=bool(_CANCELS.search(normalize_command(query, strip_polite=False))))
            self._task = asyncio.current_task()
        self._set_state(State.THINKING, query)
        log.info("Запрос: %s", private(query))
        self.ui.chat_add("user", query)
        started = time.perf_counter()
        listen_after = True
        extra: list[Reply] = []
        deferred: list = []
        try:
            reply = await self.brain.handle(query, addressed=addressed)
            # This request's own follow-ups: another request may run alongside it (one left in the background).
            extra, deferred = list(self.brain.extra), list(self.brain.deferred)
            self.brain.extra.clear()
            self.brain.deferred.clear()
            listen_after = await self._deliver(reply, started, query)
            while extra:
                listen_after = await self._deliver(extra.pop(0), started, query)
            while deferred:
                skill, intent = deferred.pop(0)
                listen_after = await self._deliver(await skill.handle(intent), started, query)
        except asyncio.CancelledError:
            log.info("Запрос отменён")
            raise
        except Exception as exc:
            log.exception("Ошибка обработки запроса")
            await self._deliver(Reply(f"Не получилось: {errors.explain(exc)}.", error=True), started, query)
        await self._wait_hold()
        if self._task is asyncio.current_task():
            self._after_answer(listen_after, expect_answer=self.brain.expects_answer())
        elif self._task is None or self._task.done():
            self._after_answer(False)   # an answer from the background, nothing else running
        elif self.state is not State.LISTENING:
            self._set_state(State.THINKING)   # the newer request is still being worked on

    def progress(self, what: str) -> None:
        """A request is being worked on: the icon stays "thinking" and the tray tooltip says what is being done."""
        self._set_state(State.THINKING, f"{what}…")

    async def _deliver(self, reply: Reply, started: float, query: str = "") -> bool:
        await self._wait_hold()
        if reply.error:
            # Said in a few words, also in the tray: the user is never left with a silent stop.
            self._earcon(earcons.ERROR)
            self.ui.notify("Джарвис: не получилось", reply.speech)
        if reply.spoken:
            return reply.listen_after
        text = ""
        shown = False
        title = card_title(query)

        def show_card(screen: str) -> None:
            nonlocal shown
            if not shown and screen.strip():
                shown = True
                self.ui.show_deck(Deck(title=title, cards=[Card("", screen.strip(), "")], done=True,
                                       linger=reading_time(screen)))

        notes: list[str] = []
        use_reaction = reply.reaction and (reply.reaction != "ok" or self.cfg.voice.ok_for_actions)
        late = asyncio.current_task() in self._background
        if late and reply.stream is None:
            await self._my_turn()
            if reply.speech:
                reply.speech = BACKGROUND_PREFIX + reply.speech
                use_reaction = False
        if use_reaction and await self.react(reply.reaction):
            text = f"[{reply.reaction}] {reply.speech}".strip()
        elif reply.stream is not None:
            stream = reply.stream.__aiter__()
            first = await self._first_token(stream, started)
            prefix = ""
            if asyncio.current_task() in self._background:
                await self._my_turn()   # the user asked something else meanwhile: let them finish first
                prefix = BACKGROUND_PREFIX

            async def timed():
                if first is None:
                    return
                if prefix:
                    yield prefix
                yield first
                async for delta in stream:
                    yield delta
                # The model is done long before the voice is: the screen part shows while the voice explains.
                show_card("".join(notes))

            # Presentation, not dictation: the card holds what is better read (formulas, numbers, steps),
            # the voice explains it; the spoken text itself is not put on screen.
            text = await self.speaker.speak(split_notes(timed(), notes))
            if not shown and text:
                self.ui.close_deck()  # the card of the previous answer is not about this one
        elif reply.speech:
            text = await self.speaker.say(reply.speech)
            if not text.startswith("[") and len(text) > self.cfg.ui.card_threshold_chars:
                show_card(answer_card_text(text))  # a long list from a module (no model to summarize it)
        screen = "".join(notes).strip()
        log.info("Ответ за %.2f с: %s%s", time.perf_counter() - started, private(text[:200]),
                 " (+ экран)" if screen else "")
        if text:
            self.ui.chat_add("assistant", text + (f"\n\n{screen}" if screen else ""))
        return reply.listen_after or reply.confirm is not None

    def show_visual_for(self, text: str):
        """A picture next to the answer when seeing beats hearing (graph, molecule, photo); fetched in the background.
        Returns what is being fetched (visuals.Request) or None: the answer's prompt then knows a picture is shown."""
        from assistant import visuals

        req = visuals.detect(text)
        if req is None:
            self.ui.close_visual()  # a new question: the previous topic's picture goes away
            return None

        async def run() -> None:
            t = time.perf_counter()
            visual = await visuals.fetch(req, self)
            if visual is not None:
                log.info("Картинка «%s» (%s) за %.1f с", private(visual.title), req.kind, time.perf_counter() - t)
                self.ui.show_visual(visual)

        asyncio.create_task(run())
        return req

    def _after_answer(self, listen_after: bool, expect_answer: bool = False) -> None:
        if self.listener and (listen_after or expect_answer) and self.mic and not self.mic.paused:
            # Hot window: follow-up without the wake word. Short delay skips the speaker echo tail.
            # After a question ("Закрыть Chrome или Яндекс Браузер?", "Удалить?") a short "да" must pass.
            source = "await" if expect_answer else "hot"

            def open_window() -> None:
                if self.listener and self.listener.mode is Mode.WAIT and self.state is not State.MUTED:
                    # With echo cancellation the last moments of audio are the user, not Jarvis's echo: a phrase
                    # begun right as he finished is kept from its first word instead of being cut.
                    clean = bool(self.mic and self.mic.aec is not None)
                    self.listener.to_await(self.cfg.assistant.hot_window_sec, source, resume=clean)
                    self._set_state(State.LISTENING, "можно без «Джарвис»")
            asyncio.get_running_loop().call_later(0.25, open_window)
        elif self.state is not State.MUTED:
            self._set_state(State.IDLE)
