"""Windows helpers: clipboard, synthetic keys, top-level windows, processes, volume."""
from __future__ import annotations

import ctypes
import logging
import os
import time
from dataclasses import dataclass

import psutil
import win32api
import win32clipboard
import win32con
import win32gui
import win32process

log = logging.getLogger("win")
user32 = ctypes.windll.user32

# ------------------------------------------------------------------ clipboard


def clipboard_seq() -> int:
    return int(user32.GetClipboardSequenceNumber())


def _open_clipboard(retries: int = 10) -> bool:
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            return True
        except Exception:  # another app holds it
            time.sleep(0.03)
    return False


def get_clipboard_text() -> str | None:
    if not _open_clipboard():
        return None
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return None
    finally:
        win32clipboard.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    if not _open_clipboard():
        return False
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        return True
    finally:
        win32clipboard.CloseClipboard()


# ------------------------------------------------------------------ keyboard

_VK = {"ctrl": win32con.VK_CONTROL, "alt": win32con.VK_MENU, "shift": win32con.VK_SHIFT, "win": win32con.VK_LWIN}
# Virtual-key codes (winuser.h); win32con does not define the media ones.
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
VK_MEDIA_NEXT, VK_MEDIA_PREV, VK_MEDIA_STOP, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB2, 0xB3
MEDIA_KEYS = {
    "play_pause": VK_MEDIA_PLAY_PAUSE, "next": VK_MEDIA_NEXT, "prev": VK_MEDIA_PREV, "stop": VK_MEDIA_STOP,
    "vol_up": VK_VOLUME_UP, "vol_down": VK_VOLUME_DOWN,
}
_EXTENDED = {VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP, VK_MEDIA_NEXT, VK_MEDIA_PREV, VK_MEDIA_STOP, VK_MEDIA_PLAY_PAUSE}


def _key(vk: int, up: bool) -> None:
    flags = win32con.KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED:
        flags |= win32con.KEYEVENTF_EXTENDEDKEY
    win32api.keybd_event(vk, 0, flags, 0)


def release_modifiers() -> None:
    """The user may still hold Ctrl/Alt from a hotkey; release them before synthetic shortcuts."""
    for vk in _VK.values():
        if win32api.GetAsyncKeyState(vk) & 0x8000:
            _key(vk, True)


def hotkey(*keys: str) -> None:
    """hotkey("ctrl", "c")"""
    vks = [_VK.get(k) or ord(k.upper()) for k in keys]
    for vk in vks:
        _key(vk, False)
    time.sleep(0.02)
    for vk in reversed(vks):
        _key(vk, True)


def press(vk: int, times: int = 1) -> None:
    for _ in range(times):
        _key(vk, False)
        _key(vk, True)
        time.sleep(0.01)


def copy_selection(timeout: float = 0.6) -> str | None:
    """Ctrl+C in the foreground window; returns the copied text, restores the old clipboard."""
    release_modifiers()
    old = get_clipboard_text()
    seq = clipboard_seq()
    hotkey("ctrl", "c")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and clipboard_seq() == seq:
        time.sleep(0.02)
    if clipboard_seq() == seq:
        return None
    text = get_clipboard_text()
    if old is not None:
        set_clipboard_text(old)
    return text


def paste_text(text: str) -> None:
    """Types text into the foreground window through the clipboard, then restores the clipboard."""
    release_modifiers()
    old = get_clipboard_text()
    set_clipboard_text(text)
    time.sleep(0.03)
    hotkey("ctrl", "v")
    time.sleep(0.25)
    if old is not None:
        set_clipboard_text(old)


# ------------------------------------------------------------------ volume


def _endpoint():
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


def set_mute(muted: bool) -> None:
    try:
        _endpoint().SetMute(1 if muted else 0, None)
    except Exception as exc:  # fallback: toggle key
        log.warning("pycaw недоступен (%s), переключаю клавишей", exc)
        press(VK_VOLUME_MUTE)


def is_muted() -> bool:
    try:
        return bool(_endpoint().GetMute())
    except Exception:
        return False


def change_volume(steps: int) -> None:
    """One step = 2% (the Windows volume key)."""
    if is_muted() and steps > 0:
        set_mute(False)
    press(MEDIA_KEYS["vol_up" if steps > 0 else "vol_down"], abs(steps))


def volume_percent() -> int | None:
    try:
        return round(_endpoint().GetMasterVolumeLevelScalar() * 100)
    except Exception:
        return None


# ------------------------------------------------------------------ windows / processes

_OWN_PIDS = {os.getpid(), os.getppid()}
_PROTECTED = {"explorer.exe", "csrss.exe", "winlogon.exe", "dwm.exe", "svchost.exe", "lsass.exe", "services.exe",
              "smss.exe", "wininit.exe", "system", "taskmgr.exe", "searchhost.exe", "startmenuexperiencehost.exe",
              "shellexperiencehost.exe", "textinputhost.exe", "applicationframehost.exe", "ollama.exe",
              "ollama app.exe"}


_FRIENDLY = {
    "browser.exe": "Яндекс Браузер", "chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox",
    "telegram.exe": "Telegram", "discord.exe": "Discord", "steam.exe": "Steam", "steamwebhelper.exe": "Steam",
    "code.exe": "VS Code", "explorer.exe": "Проводник", "notepad.exe": "Блокнот", "winword.exe": "Word",
    "excel.exe": "Excel", "claude.exe": "Claude", "obs64.exe": "OBS", "spotify.exe": "Spotify",
}


@dataclass
class Window:
    hwnd: int
    title: str
    pid: int
    exe: str  # "telegram.exe"

    @property
    def app(self) -> str:
        return self.exe.removesuffix(".exe")

    @property
    def friendly(self) -> str:
        """Human name for speech: UWP apps live in ApplicationFrameHost, so their title is used."""
        if self.exe in ("applicationframehost.exe", "systemsettings.exe"):
            return self.title
        return _FRIENDLY.get(self.exe, self.app)


def _exe(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.Error, OSError):
        return ""


def list_windows() -> list[Window]:
    """Visible, titled, top-level app windows (no tool windows, no our own)."""
    out: list[Window] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TOOLWINDOW:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid in _OWN_PIDS:
            return True
        exe = _exe(pid)
        if exe and exe not in ("textinputhost.exe", "searchhost.exe", "startmenuexperiencehost.exe"):
            out.append(Window(hwnd, title, pid, exe))
        return True

    win32gui.EnumWindows(cb, None)
    return out


def foreground_window() -> Window | None:
    hwnd = win32gui.GetForegroundWindow()
    for w in list_windows():
        if w.hwnd == hwnd:
            return w
    return None


def close_window(w: Window) -> None:
    win32gui.PostMessage(w.hwnd, win32con.WM_CLOSE, 0, 0)


def minimize(w: Window) -> None:
    win32gui.ShowWindow(w.hwnd, win32con.SW_MINIMIZE)


def maximize(w: Window) -> None:
    win32gui.ShowWindow(w.hwnd, win32con.SW_MAXIMIZE)


def activate(w: Window) -> None:
    if win32gui.IsIconic(w.hwnd):
        win32gui.ShowWindow(w.hwnd, win32con.SW_RESTORE)
    # Windows only lets the foreground process move focus; a synthetic Alt press unlocks it.
    _key(win32con.VK_MENU, False)
    try:
        win32gui.SetForegroundWindow(w.hwnd)
    except Exception:
        win32gui.BringWindowToTop(w.hwnd)
    finally:
        _key(win32con.VK_MENU, True)


def minimize_all() -> None:
    release_modifiers()
    hotkey("win", "d")


def is_protected(exe: str) -> bool:
    return exe.lower() in _PROTECTED


def kill_process_tree(exe: str) -> int:
    """Force-terminates every process with this image name. Returns how many were killed."""
    if is_protected(exe):
        return 0
    n = 0
    for p in psutil.process_iter(["name", "pid"]):
        if (p.info["name"] or "").lower() == exe.lower() and p.info["pid"] not in _OWN_PIDS:
            try:
                p.kill()
                n += 1
            except psutil.Error:
                pass
    return n
