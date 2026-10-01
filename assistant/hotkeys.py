"""Global hotkeys via the Windows RegisterHotKey API.

Works by virtual-key codes, so it does not depend on the keyboard layout (pynput compared characters and
silently failed with the Russian layout and Ctrl held). Hold-to-dictate: the press comes from WM_HOTKEY,
the release is detected by polling the key state.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import Callable

log = logging.getLogger("hotkeys")
user32 = ctypes.windll.user32

MOD = {"<ctrl>": 0x0002, "<alt>": 0x0001, "<shift>": 0x0004, "<cmd>": 0x0008, "<win>": 0x0008}
MOD_VK = {0x0002: 0x11, 0x0001: 0x12, 0x0004: 0x10, 0x0008: 0x5B}  # modifier flag -> VK_CONTROL/MENU/SHIFT/LWIN
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
NAMED_VK = {"<space>": 0x20, "<enter>": 0x0D, "<esc>": 0x1B, "<tab>": 0x09, "<f1>": 0x70, "<f2>": 0x71, "<f3>": 0x72,
            "<f4>": 0x73, "<f5>": 0x74, "<f6>": 0x75, "<f7>": 0x76, "<f8>": 0x77, "<f9>": 0x78, "<f10>": 0x79,
            "<f11>": 0x7A, "<f12>": 0x7B}


def parse(combo: str) -> tuple[int, int]:
    """'<ctrl>+<alt>+j' -> (MOD_CONTROL | MOD_ALT, VK_J)."""
    mods, vk = 0, 0
    for part in combo.lower().replace(" ", "").split("+"):
        if part in MOD:
            mods |= MOD[part]
        elif part in NAMED_VK:
            vk = NAMED_VK[part]
        elif len(part) == 1 and (part.isalnum()):
            vk = ord(part.upper())
        else:
            raise ValueError(f"не понимаю клавишу «{part}» в «{combo}»")
    if not vk:
        raise ValueError(f"нет основной клавиши в «{combo}»")
    return mods, vk


def is_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class Hotkeys:
    def __init__(self) -> None:
        self._press: dict[int, Callable[[], None]] = {}
        self._hold: dict[int, tuple[Callable[[], None], Callable[[], None], int, int]] = {}
        self._combos: dict[int, tuple[str, int, int]] = {}
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._next_id = 1
        self.failed: list[str] = []

    def on_press(self, combo: str, callback: Callable[[], None]) -> None:
        hid = self._add(combo)
        if hid:
            self._press[hid] = callback

    def on_hold(self, combo: str, start: Callable[[], None], stop: Callable[[], None]) -> None:
        hid = self._add(combo)
        if hid:
            mods, vk = self._combos[hid][1:]
            self._hold[hid] = (start, stop, mods, vk)

    def _add(self, combo: str) -> int:
        try:
            mods, vk = parse(combo)
        except ValueError as exc:
            log.warning("Горячая клавиша: %s", exc)
            self.failed.append(combo)
            return 0
        hid = self._next_id
        self._next_id += 1
        self._combos[hid] = (combo, mods, vk)
        return hid

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="hotkeys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _loop(self) -> None:
        # RegisterHotKey binds hotkeys to the calling thread's message queue.
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        for hid, (combo, mods, vk) in self._combos.items():
            if user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                log.info("Горячая клавиша %s", combo)
            else:
                log.warning("Сочетание %s занято другой программой — поменяйте его в настройках", combo)
                self.failed.append(combo)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message != WM_HOTKEY:
                continue
            hid = int(msg.wParam)
            try:
                if hid in self._press:
                    self._press[hid]()
                elif hid in self._hold:
                    self._run_hold(*self._hold[hid])
            except Exception:
                log.exception("Ошибка обработчика горячей клавиши")
        for hid in self._combos:
            user32.UnregisterHotKey(None, hid)

    @staticmethod
    def _run_hold(start: Callable[[], None], stop: Callable[[], None], mods: int, vk: int) -> None:
        start()

        def watch() -> None:
            keys = [vk] + [MOD_VK[m] for m in MOD_VK if mods & m]
            while all(is_down(k) for k in keys):
                time.sleep(0.03)
            stop()

        threading.Thread(target=watch, name="hotkey-hold", daemon=True).start()
