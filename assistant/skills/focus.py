"""Pomodoro: work / break cycles with spoken transitions. "помодоро", "помодоро на 50 минут", "стоп помодоро"."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from assistant.audio import earcons
from assistant.nlu import parse_duration, say_duration, sleep_until, words_to_numbers
from assistant.skills.base import Intent, Reply, Skill, Tool

log = logging.getLogger("focus")

_NAME = r"(помодоро|pomodoro|помидор\w*|фокус\w*|фокус-сесси\w*|рабоч\w+ сесси\w*|сесси\w* фокуса|таймер фокуса|режим фокуса)"
_STOP = re.compile(rf"\b(останови|выключи|отмени|заверши|закончи|стоп|хватит|прекрати|отключи|сбрось)\w*\s+{_NAME}|^{_NAME} (стоп|хватит|отмена|выключи)$")
_STATUS = re.compile(rf"(сколько (еще |мне )?(осталось|работать)|когда перерыв|статус)\s*.*{_NAME}|^сколько до перерыва|^когда (будет )?перерыв")
_START = re.compile(rf"^(запусти|начни|включи|давай|старт|поставь|начинаем|погнали)?\s*{_NAME}\b|^(поработаем|давай поработаем|время работать) по помодоро")


class FocusSkill(Skill):
    name = "focus"
    title = "Помодоро"
    examples = [
        'помодоро',
        'помодоро на <N> минут',
        'останови помодоро',
        'сколько до перерыва',
    ]

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._phase = ""
        self._phase_end = 0.0

    def match(self, text: str) -> Intent | None:
        if _STOP.search(text):
            return Intent(self.name, "stop", text=text)
        if _STATUS.search(text):
            return Intent(self.name, "status", text=text)
        if _START.search(text):
            parsed = parse_duration(words_to_numbers(text))
            return Intent(self.name, "start", {"work_min": parsed[0] // 60 if parsed else 0}, text)
        return None

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
        cfg = self.app.cfg.focus
        work = int(intent.slots.get("work_min") or cfg.work_min)
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
        cfg = self.app.cfg.focus
        try:
            for rnd in range(1, cfg.rounds + 1):
                await self._phase_wait("работа", work)
                last = rnd == cfg.rounds
                rest = cfg.long_break_min if last else cfg.break_min
                self.app.ui.notify("Помодоро", f"Перерыв {rest} минут")
                await self.app.announce(f"Перерыв {rest} минут. Встаньте, разомнитесь.", earcons.ALARM)
                await self._phase_wait("перерыв", rest)
                if last:
                    await self.app.announce(f"Цикл из {cfg.rounds} помодоро завершён. Отличная работа.", earcons.DONE)
                    return
                self.app.ui.notify("Помодоро", "Работаем")
                await self.app.announce(f"Перерыв окончен. Раунд {rnd + 1}, работаем {work} минут.", earcons.ALARM)
        except asyncio.CancelledError:
            log.info("Помодоро остановлен")
        finally:
            self._phase = ""

    async def stop(self) -> None:
        if self.running:
            self._task.cancel()


def create() -> Skill:
    return FocusSkill()
