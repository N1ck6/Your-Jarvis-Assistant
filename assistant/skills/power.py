"""Power: lock the PC at once; sleep, shut down and restart only after a spoken "да"; shutdown can be delayed
("выключи компьютер через час") and cancelled ("отмени выключение"). Uses the standard Windows `shutdown` tool."""
from __future__ import annotations

import ctypes
import re
import subprocess

from assistant.nlu import parse_duration, say_duration, words_to_numbers
from assistant.skills.base import Intent, Reply, Skill, run_blocking

_PC = r"(?:компьютер|комп|пк|ноутбук|ноут|систему)"
_LOCK = re.compile(rf"^(?:заблокируй|заблочь|блокируй|запри)(?:\s+{_PC}|\s+экран)?$|^блокировка( экрана)?$")
_SLEEP = re.compile(rf"^(?:усыпи|отправь в сон|переведи в сон|уложи спать)(?:\s+{_PC})?$|^(?:спящий режим|режим сна)$|"
                    rf"^(?:переведи|отправь)\s+{_PC}\s+в\s+(?:сон|спящий режим)$")
_OFF = re.compile(rf"^(?:выключи|выключить|отключи|заверши работу)\s+{_PC}(?P<rest>.*)$|^(?:выключение|завершение работы)( {_PC})?(?P<rest2>.*)$")
_REBOOT = re.compile(rf"^(?:перезагрузи|перезапусти|ребутни)\s+{_PC}(?P<rest>.*)$|^перезагрузка( {_PC})?(?P<rest2>.*)$")
_CANCEL = re.compile(r"^(?:отмени|отменить|останови|не надо)\s+(?:выключение|перезагрузку|завершение работы)( компьютера)?$|"
                     r"^не выключай( компьютер)?$")

_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _shutdown(*args: str) -> int:
    return subprocess.run(["shutdown", *args], capture_output=True, creationflags=_flags).returncode


class PowerSkill(Skill):
    name = "power"
    title = "Питание компьютера"
    examples = [
        'заблокируй компьютер',
        'спящий режим',
        'выключи компьютер через <N> минут',
        'перезагрузи компьютер',
        'отмени выключение',
    ]

    def match(self, text: str) -> Intent | None:
        if _LOCK.match(text):
            return Intent(self.name, "lock", text=text)
        if _SLEEP.match(text):
            return Intent(self.name, "sleep", text=text)
        if _CANCEL.match(text):
            return Intent(self.name, "cancel", text=text)
        for action, pattern in (("off", _OFF), ("reboot", _REBOOT)):
            if m := pattern.match(text):
                rest = (m.group("rest") or m.group("rest2") or "").strip()
                parsed = parse_duration(words_to_numbers(rest)) if rest else None
                if rest and not parsed:
                    return None  # "выключи компьютерную игру"
                return Intent(self.name, action, {"delay": parsed[0] if parsed else 30}, text)
        return None

    async def handle(self, intent: Intent) -> Reply:
        if intent.action == "lock":
            await run_blocking(ctypes.windll.user32.LockWorkStation)
            return Reply(listen_after=False)
        if intent.action == "cancel":
            code = await run_blocking(_shutdown, "/a")
            return Reply("Выключение отменено." if code == 0 else "Выключение не было запланировано.", listen_after=False)
        if intent.action == "sleep":
            async def sleep() -> Reply:
                # SetSuspendState(hibernate=False, force=False, disable_wake_events=False)
                await run_blocking(ctypes.windll.powrprof.SetSuspendState, 0, 0, 0)
                return Reply(spoken=True, listen_after=False)

            return Reply("Перевести компьютер в сон?", confirm=sleep)
        delay = int(intent.slots.get("delay") or 30)
        verb = "Выключить" if intent.action == "off" else "Перезагрузить"
        flag = "/s" if intent.action == "off" else "/r"

        async def do() -> Reply:
            await run_blocking(_shutdown, flag, "/t", str(delay))

            async def undo() -> Reply:
                await run_blocking(_shutdown, "/a")
                return Reply("Выключение отменено.")

            self.app.brain.last_undo = undo
            when = "через полминуты" if delay == 30 else f"через {say_duration(delay)}"
            return Reply(f"Хорошо, {when}. Отменить: «отмени выключение».", listen_after=False)

        when = "" if delay == 30 else f" через {say_duration(delay)}"
        return Reply(f"{verb} компьютер{when}? Несохранённая работа пропадёт.", confirm=do)


def create() -> Skill:
    return PowerSkill()
