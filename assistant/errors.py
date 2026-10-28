"""What went wrong, in a few words the user understands: spoken and shown instead of a silent stop or a traceback.

The traceback still goes to the log; the user hears "Не получилось включить музыку: нет звукового устройства."
"""
from __future__ import annotations

import asyncio
import errno


def explain(exc: BaseException) -> str:
    """A short reason, without the exception's own text (it is English and technical)."""
    for e in _chain(exc):
        name = type(e).__name__
        module = type(e).__module__
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)) or "Timeout" in name:
            return "ответ не пришёл вовремя"
        if isinstance(e, FileNotFoundError):
            return f"не нашёл файл «{_short(e.filename)}»" if e.filename else "не нашёл нужный файл"
        if isinstance(e, PermissionError):
            return "нет доступа к файлу или папке"
        if module.startswith("miniaudio") or name in ("PortAudioError", "MiniaudioError"):
            return "не открылось звуковое устройство"
        if "Connect" in name or isinstance(e, ConnectionError) or (
                isinstance(e, OSError) and e.errno in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH)):
            return "нет связи с интернетом или нужной программой"
        if module.startswith(("httpx", "aiohttp", "google", "openai", "groq")):
            return "сервис в интернете ответил ошибкой"
        if module.startswith("ollama"):
            return "локальная модель не ответила — запущена ли Ollama?"
        if isinstance(e, MemoryError):
            return "не хватило памяти"
    return "внутренняя ошибка"


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _short(path) -> str:
    text = str(path).replace("\\", "/")
    return text.rsplit("/", 1)[-1] or text
