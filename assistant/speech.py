"""Speaker: turns text (or a token stream) into sentences, synthesizes ahead and plays in order."""
from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterable, AsyncIterator, Callable

from assistant.audio.player import AudioClip, Player
from assistant.tts.manager import TtsManager

log = logging.getLogger("speech")

_SENTENCE_END = re.compile(r"(.+?[.!?…]+)(?=\s|$)|(.+?)\n", re.S)
_SOFT_BREAK = re.compile(r"[,;:—]\s")


class SentenceSplitter:
    """Feeds on text deltas, yields speakable chunks as soon as they are complete."""

    def __init__(self, first_soft_limit: int = 70, hard_limit: int = 220) -> None:
        self.buf = ""
        self.emitted = 0
        self.first_soft_limit = first_soft_limit
        self.hard_limit = hard_limit

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out: list[str] = []
        while True:
            m = _SENTENCE_END.match(self.buf)
            if m and m.end() < len(self.buf):  # need one char of lookahead ("3.5" is not an end)
                chunk = (m.group(1) or m.group(2) or "").strip()
                self.buf = self.buf[m.end():]
                if chunk:
                    out.append(chunk)
                continue
            limit = self.first_soft_limit if self.emitted + len(out) == 0 else self.hard_limit
            if len(self.buf) > limit:
                cut = None
                for sm in _SOFT_BREAK.finditer(self.buf):
                    if sm.end() >= 25:
                        cut = sm.end()
                        if cut >= limit * 0.6:
                            break
                if cut is None and len(self.buf) > self.hard_limit:
                    cut = self.buf.rfind(" ", 0, self.hard_limit) or self.hard_limit
                if cut:
                    out.append(self.buf[:cut].strip())
                    self.buf = self.buf[cut:]
                    continue
            break
        self.emitted += len(out)
        return [c for c in out if c.strip(" .,")]

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest.strip(" .,") else []


async def _as_stream(text: str) -> AsyncIterator[str]:
    yield text


class Speaker:
    def __init__(self, tts: TtsManager, player: Player,
                 on_start: Callable[[], None] = lambda: None, on_end: Callable[[], None] = lambda: None) -> None:
        self.tts = tts
        self.player = player
        self.on_start = on_start
        self.on_end = on_end
        self._synth_pool = ThreadPoolExecutor(1, thread_name_prefix="tts")
        self._play_pool = ThreadPoolExecutor(1, thread_name_prefix="play")
        self._gen = 0
        self._lock = asyncio.Lock()
        self.speaking = False
        self.last_text = ""
        self.current_text = ""  # sentence being played right now (echo check for stop words)

    def stop(self) -> None:
        self._gen += 1
        self.player.stop()

    async def say(self, text: str) -> str:
        return await self.speak(_as_stream(text))

    async def play_clip(self, clip: AudioClip, text: str = "") -> bool:
        """Plays a recorded clip in the same queue as speech. Returns False if interrupted."""
        async with self._lock:
            gen = self._gen
            loop = asyncio.get_running_loop()
            self._begin(text)
            try:
                return await loop.run_in_executor(self._play_pool, self.player.play, clip) and gen == self._gen
            finally:
                self._end()

    def _begin(self, text: str) -> None:
        self.current_text = text
        if not self.speaking:
            self.speaking = True
            self.on_start()

    def _end(self) -> None:
        self.current_text = ""
        if self.speaking:
            self.speaking = False
            self.on_end()

    async def speak(self, source: AsyncIterable[str]) -> str:
        """Speaks everything the source yields. Returns the text that was produced."""
        async with self._lock:
            gen = self._gen
            loop = asyncio.get_running_loop()
            audio_q: asyncio.Queue[tuple[AudioClip, str] | None] = asyncio.Queue()
            spoken: list[str] = []

            async def produce() -> None:
                splitter = SentenceSplitter()
                try:
                    async for delta in source:
                        if gen != self._gen:
                            return
                        spoken.append(delta)
                        for sentence in splitter.feed(delta):
                            await self._synth_into(loop, sentence, audio_q, gen)
                    for sentence in splitter.flush():
                        await self._synth_into(loop, sentence, audio_q, gen)
                finally:
                    await audio_q.put(None)
                    if hasattr(source, "aclose"):
                        await source.aclose()  # type: ignore[union-attr]

            async def consume() -> None:
                while True:
                    item = await audio_q.get()
                    if item is None or gen != self._gen:
                        break
                    clip, sentence = item
                    self._begin(sentence)
                    await loop.run_in_executor(self._play_pool, self.player.play, clip)

            try:
                await asyncio.gather(produce(), consume())
            finally:
                self._end()
            text = "".join(spoken).strip()
            if text:
                self.last_text = text
            return text

    async def _synth_into(self, loop, sentence: str, q: asyncio.Queue, gen: int) -> None:
        if gen != self._gen:
            return
        try:
            clip = await loop.run_in_executor(self._synth_pool, self.tts.synth, sentence)
        except Exception:
            log.exception("Ошибка синтеза: %s", sentence[:80])
            return
        if clip.samples.size:
            await q.put((clip, sentence))

    def shutdown(self) -> None:
        self.stop()
        self._synth_pool.shutdown(wait=False, cancel_futures=True)
        self._play_pool.shutdown(wait=False, cancel_futures=True)
