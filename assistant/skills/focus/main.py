"""Pomodoro: work / break cycles with spoken transitions. Phrases: commands.toml, settings: [config] of skill.toml."""
from __future__ import annotations

import asyncio
import logging
import time

from assistant.audio import earcons
from assistant.nlu import parse_duration, say_duration, sleep_until, words_to_numbers
from assistant.skills.base import Intent, Reply, Skill, Tool

log = logging.getLogger("focus")


class FocusSkill(Skill):
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._phase = ""
        self._phase_end = 0.0

    def tools(self) -> list[Tool]:
        return [Tool("pomodoro", "Запустить помодоро (работа/перерыв) или остановить.",
                     {"type": "object", "properties": {"action": {"type": "string", "enum": ["start", "stop"]},
                                                       "work_min": {"type": "integer"}}, "required": ["action"]}, "tool")]

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def handle(self, intent: Intent) -> Reply:
        action = intent.slots.get("action") if intent.action == "tool" else intent.action
        if action == "stop":
            if not self.running:
                return Reply("Помодоро не запущен.")
            self._task.cancel()
            return Reply("Помодоро остановлен.", listen_after=False)
        if action == "status":
            if not self.running:
                return Reply("Помодоро не запущен.")
            left = max(0, int(self._phase_end - time.time()))
            return Reply(f"{self._phase.capitalize()}: осталось {say_duration(left)}.")
        cfg = self.ctx.config
        parsed = parse_duration(words_to_numbers(intent.text)) if intent.action == "start" else None
        work = int(intent.slots.get("work_min") or (parsed[0] // 60 if parsed else 0) or cfg.work_min)
        if self.running:
            self._task.cancel()
        self._task = asyncio.create_task(self._run(work))

        async def undo() -> Reply:
            if self.running:
                self._task.cancel()
            return Reply("Помодоро остановлен.")

        return Reply(f"Помодоро: {work} минут работы, потом {cfg.break_min} минут отдыха. Не отвлекаю.",
                     listen_after=False, undo=undo)

    async def _phase_wait(self, name: str, minutes: int) -> None:
        self._phase = name
        self._phase_end = time.time() + minutes * 60
        await sleep_until(self._phase_end)

    async def _run(self, work: int) -> None:
        cfg = self.ctx.config
        try:
            for rnd in range(1, cfg.rounds + 1):
                await self._phase_wait("работа", work)
                last = rnd == cfg.rounds
                rest = cfg.long_break_min if last else cfg.break_min
                self.ctx.notify("Помодоро", f"Перерыв {rest} минут")
                await self.ctx.say(f"Перерыв {rest} минут. Встаньте, разомнитесь.", earcons.ALARM)
                await self._phase_wait("перерыв", rest)
                if last:
                    await self.ctx.say(f"Цикл из {cfg.rounds} помодоро завершён. Отличная работа.", earcons.DONE)
                    return
                self.ctx.notify("Помодоро", "Работаем")
                await self.ctx.say(f"Перерыв окончен. Раунд {rnd + 1}, работаем {work} минут.", earcons.ALARM)
        except asyncio.CancelledError:
            log.info("Помодоро остановлен")
        finally:
            self._phase = ""

    async def stop(self) -> None:
        if self.running:
            self._task.cancel()


def create() -> Skill:
    return FocusSkill()
