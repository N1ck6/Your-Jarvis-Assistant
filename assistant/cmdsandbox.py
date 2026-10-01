"""Read-only cmd commands for the model: a strict whitelist, no redirection, no chaining, allowed folders only.

The model may ask e.g. `dir /b /s "C:\\Users\\me\\Downloads\\*.pdf"` or `ipconfig`. Anything that could change
files or system state (del, copy, >, |, &, ipconfig /release, ...) is refused before cmd starts.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from assistant.config import Settings
from assistant.fsaccess import allowed_roots, is_allowed

# command -> whether its non-flag arguments are paths that must stay inside the allowed folders
WHITELIST: dict[str, bool] = {
    "dir": True, "tree": True, "type": True, "where": False, "findstr": True, "find": True,
    "ipconfig": False, "systeminfo": False, "tasklist": False, "hostname": False, "ver": False, "whoami": False,
    "getmac": False, "vol": False, "netstat": False, "ping": False, "nslookup": False, "driverquery": False,
}
FORBIDDEN_CHARS = set("<>|&^%!`\r\n;")
FORBIDDEN_FLAGS = {"ipconfig": {"/release", "/renew", "/flushdns", "/registerdns", "/release6", "/renew6", "/setclassid"},
                   "ping": {"-t", "/t", "-l"}, "netstat": {"-t"}}
MAX_OUTPUT = 3500


class Refused(Exception):
    pass


def check(cfg: Settings, command: str) -> list[str]:
    """Returns tokens if the command is allowed, raises Refused otherwise."""
    command = command.strip()
    if not command:
        raise Refused("пустая команда")
    bad = FORBIDDEN_CHARS & set(command)
    if bad:
        raise Refused(f"запрещённые символы: {''.join(sorted(bad))}")
    if command.count('"') % 2:
        raise Refused("незакрытые кавычки")
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    name = tokens[0].lower().removesuffix(".exe")
    if name not in WHITELIST:
        raise Refused(f"команда {name} не входит в список разрешённых (только чтение)")
    for flag in tokens[1:]:
        if flag.lower() in FORBIDDEN_FLAGS.get(name, set()):
            raise Refused(f"флаг {flag} меняет состояние системы")
    if name == "ping":
        tokens = [t for t in tokens if t.lower() not in ("-n", "/n") and not t.isdigit()] + ["-n", "3"]
    if WHITELIST[name]:
        args = [t.strip('"') for t in tokens[1:] if not t.startswith(("/", "-"))]
        if name in ("findstr", "find") and args:
            args = args[1:]  # the first argument is the search pattern
        if not args and name in ("dir", "tree"):
            raise Refused("укажите папку")
        for arg in args:
            base = Path(arg.replace("*", "x").replace("?", "x"))
            if not base.is_absolute():
                raise Refused(f"нужен полный путь: {arg}")
            if not is_allowed(cfg, base.parent if any(c in arg for c in "*?") else base):
                raise Refused(f"папка не входит в разрешённые: {arg}")
    return tokens


def run(cfg: Settings, command: str) -> str:
    if not cfg.files.allow_cmd:
        return "Выполнение команд отключено в настройках."
    try:
        tokens = check(cfg, command)
    except Refused as exc:
        return f"Отказано: {exc}."
    roots = allowed_roots(cfg)
    try:
        # A string goes to CreateProcess verbatim: a list would re-escape the quotes as \" which cmd does not
        # understand. Tokens keep their quotes (shlex posix=False) and were checked above.
        proc = subprocess.run("cmd /d /q /c " + " ".join(tokens),
                              cwd=str(roots[0]) if roots else None, capture_output=True, timeout=15,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return "Команда выполнялась слишком долго и была остановлена."
    raw = proc.stdout or proc.stderr
    for enc in ("cp866", "utf-8", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + f"\n… (обрезано, всего {len(text)} символов)"
    return text or "(пустой вывод)"
