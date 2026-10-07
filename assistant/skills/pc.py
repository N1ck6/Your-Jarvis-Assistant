"""Computer status and read-only files: disk, memory, CPU hogs, uptime, battery, IP; count / list / size / find
files in allowed folders; a whitelisted read-only cmd for the model. Never edits, moves or deletes anything."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import socket
import time

import psutil

from assistant import cmdsandbox, fsaccess
from assistant.core import Card, Deck
from assistant.nlu import plural
from assistant.log import private
from assistant.skills.base import Intent, Reply, Skill, Tool, run_blocking

log = logging.getLogger("pc")

_KIND = (r"(?P<kind>файл\w*|фото\w*|фотографи\w*|картин\w*|изображени\w*|видео\w*|ролик\w*|документ\w*|pdf|пдф|"
         r"архив\w*|программ\w*|установщик\w*)")
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("disk", re.compile(r"(сколько|много ли|осталось ли|хватает ли|хватит ли) (мне )?(свободного |свободно )?(места|памяти) (на |осталось на )?(диске|дисках|компьютере|компе|ссд|ssd|винчестере)|"
                        r"^(свободное место|место на диске|сколько места)$|(кончается|заканчивается|забит|переполнен)\w* ли (место|диск)|^хватает ли (мне )?места")),
    ("ram", re.compile(r"(сколько|какая) (свободн\w* |занят\w* |всего )?(оперативн\w+|оперативки|озу|памяти ram)|загрузка (памяти|оперативки)|"
                       r"сколько (оперативки|оперативной памяти) (свободно|занято)|"
                       r"^сколько (свободной )?памяти$")),
    ("cpu", re.compile(r"загрузка процессора|загружен ли процессор|что (грузит|тормозит|жрет|нагружает) (компьютер|процессор|память|комп|систему)|"
                       r"почему (тормозит|лагает|виснет) (компьютер|комп)|какие процессы (грузят|жрут|больше всего)")),
    ("uptime", re.compile(r"сколько (времени )?(работает|включен) (компьютер|комп)|время работы (компьютера|системы)|когда (я )?включил компьютер")),
    ("battery", re.compile(r"заряд (батареи|ноутбука|аккумулятора)|сколько (заряда|процентов батареи)|^батарея$")),
    ("ip", re.compile(r"какой (у меня )?(ip|айпи|ай пи)( адрес)?|^мой (ip|айпи)|локальный (ip|адрес)")),
    ("mac", re.compile(r"\b(mac|мак|мас)[ -]?адрес|физический адрес")),
    ("count", re.compile(rf"^сколько (у меня )?{_KIND}( всего)? (в|во|на) (папке |папка )?(?P<dir>.+)$")),
    ("recent", re.compile(r"^(что|какие файлы|покажи файлы|последние файлы|последний файл|что нового|что лежит) (есть )?(в|во|на) (папке )?(?P<dir>.+)$|"
                          r"^(последние|недавние|свежие) (загрузки|скачанные файлы|файлы)$|^что я (скачал|загрузил)( последним| недавно)?$")),
    ("size", re.compile(r"^сколько (весит|места занимает|занимает места|занимает) (папка )?(?P<dir>.+)$")),
    ("open_found", re.compile(r"^открой (его|ее|этот файл|найденный файл|файл|первый|второй|третий|первый файл|второй файл|третий файл)$")),
    ("find", re.compile(r"^(найди|поищи|ищи|где) (мой |мою )?(файл|документ|документы|фото|папку|презентацию|таблицу) (?P<name>.+)$|"
                        r"^где (у меня )?(лежит|находится|лежат) (файл |документ )?(?P<name2>.+)$")),
    ("open_dir", re.compile(r"^открой (папку|папка) (?P<dir>.+)$|^открой (загрузки|документы|рабочий стол|папку загрузок)$")),
]
_ORDINAL = {"первый": 0, "второй": 1, "третий": 2}


class PcSkill(Skill):
    name = "pc"
    title = "Компьютер и файлы"
    examples = [
        "сколько места на диске", "сколько свободно оперативной памяти", "что грузит компьютер",
        "сколько работает компьютер", "заряд батареи", "какой у меня ip (только IP)", "какой у меня mac адрес",
        "сколько <файлов|фото|видео|документов|pdf> в <папка>", "что в <папка> (последние файлы)",
        "сколько весит папка <папка>", "найди файл <имя>", "открой его (найденный файл)", "открой папку <папка>",
    ]

    def __init__(self) -> None:
        self.found: list[fsaccess.Found] = []

    def match(self, text: str) -> Intent | None:
        for action, pattern in _PATTERNS:
            m = pattern.search(text)
            if m:
                g = {k: v for k, v in m.groupdict().items() if v}
                slots = {"dir": g.get("dir", ""), "kind": g.get("kind", ""), "name": g.get("name") or g.get("name2", "")}
                if action == "open_dir" and not slots["dir"]:
                    slots["dir"] = text.removeprefix("открой ").strip()
                if action == "recent" and not slots["dir"]:
                    slots["dir"] = "загрузки"
                if action == "open_found":
                    slots["index"] = next((i for w, i in _ORDINAL.items() if w in text), 0)
                    if not self.found:
                        return None  # "открой его" without a search: let others decide
                if action in ("count", "recent", "size", "open_dir") and not self._is_folder(slots["dir"], text):
                    return None  # "что на улице" is weather, not a folder
                return Intent(self.name, action, slots, text)
        return None

    def _is_folder(self, spoken: str, text: str) -> bool:
        spoken = spoken.strip()
        if spoken in fsaccess.SPOKEN or " папк" in f" {text}" or spoken.startswith(("музык", "замет")):
            return True
        return fsaccess.resolve_dir(self.app.cfg, spoken) is not None

    def tools(self) -> list[Tool]:
        return [
            Tool("files_query", "Только чтение файлов в разрешённых папках: сосчитать, последние файлы, размер папки, "
                 "найти файл по имени.",
                 {"type": "object", "properties": {
                     "action": {"type": "string", "enum": ["count", "recent", "size", "find"]},
                     "folder": {"type": "string", "description": "загрузки, документы, рабочий стол, музыка, фото, видео или имя папки"},
                     "kind": {"type": "string", "description": "файлы, фото, видео, документы, pdf, архивы, программы"},
                     "name": {"type": "string", "description": "имя файла для поиска"}},
                  "required": ["action"]}, "tool", direct=False),
            Tool("system_status", "Состояние компьютера: диск, память, процессор, время работы, батарея, IP.",
                 {"type": "object", "properties": {"what": {"type": "string", "enum": ["disk", "ram", "cpu", "uptime", "battery", "ip"]}},
                  "required": ["what"]}, "status", direct=False),
            Tool("readonly_cmd", "Выполнить команду cmd ТОЛЬКО для чтения (dir, tree, type, findstr, where, ipconfig, "
                 "systeminfo, tasklist, netstat, ping). Пути — полные и только в разрешённых папках. Ничего не изменяет.",
                 {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                 "cmd", direct=False),
        ]

    # ------------------------------------------------------------------ actions
    async def handle(self, intent: Intent) -> Reply:
        s = intent.slots
        action = intent.action
        if action == "tool":
            action = s.get("action", "")
            s = {"dir": s.get("folder", ""), "kind": s.get("kind", ""), "name": s.get("name", "")}
        elif action == "status":
            action = s.get("what", "")
        if action == "cmd":
            out = await run_blocking(cmdsandbox.run, self.app.cfg, str(intent.slots.get("command", "")))
            log.info("cmd: %s -> %d символов", private(intent.slots.get("command")), len(out))
            return Reply(tool_result=out)
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return Reply("Не понял, что проверить.")
        return await handler(s)

    async def _do_disk(self, s) -> Reply:
        parts = []
        for p in psutil.disk_partitions(all=False):
            if "cdrom" in p.opts or not p.fstype:
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            parts.append(f"на диске {p.device[0]} свободно {fsaccess.human_size(u.free)} из {fsaccess.human_size(u.total)}")
        text = "; ".join(parts) or "Не удалось прочитать диски"
        return Reply(text[:1].upper() + text[1:] + ".", tool_result=text)

    async def _do_ram(self, s) -> Reply:
        m = psutil.virtual_memory()
        text = (f"Занято {fsaccess.human_size(m.used)} из {fsaccess.human_size(m.total)}, "
                f"это {round(m.percent)}%. Свободно {fsaccess.human_size(m.available)}.")
        return Reply(text, tool_result=text)

    async def _do_cpu(self, s) -> Reply:
        def measure():
            procs = list(psutil.process_iter(["name", "memory_info"]))
            for p in procs:
                try:
                    p.cpu_percent(None)
                except psutil.Error:
                    pass
            total = psutil.cpu_percent(interval=0.7)
            rows = []
            for p in procs:
                try:
                    rows.append((p.cpu_percent(None) / psutil.cpu_count(), p.info["memory_info"].rss, p.info["name"]))
                except (psutil.Error, AttributeError):
                    pass
            return total, rows

        total, rows = await run_blocking(measure)
        top_cpu = sorted(rows, reverse=True)[:3]
        top_mem = sorted(rows, key=lambda r: r[1], reverse=True)[:3]
        cpu_txt = ", ".join(f"{n.removesuffix('.exe')} {round(c)}%" for c, _, n in top_cpu if c >= 1)
        mem_txt = ", ".join(f"{n.removesuffix('.exe')} {fsaccess.human_size(m)}" for _, m, n in top_mem)
        text = f"Процессор загружен на {round(total)}%." + (f" Больше всего: {cpu_txt}." if cpu_txt else "") + \
            f" По памяти: {mem_txt}."
        return Reply(text, tool_result=text)

    async def _do_uptime(self, s) -> Reply:
        up = int(time.time() - psutil.boot_time())
        h, m = divmod(up // 60, 60)
        d, h = divmod(h, 24)
        parts = ([f"{d} {plural(d, 'день', 'дня', 'дней')}"] if d else []) + \
                ([f"{h} {plural(h, 'час', 'часа', 'часов')}"] if h else []) + [f"{m} {plural(m, 'минуту', 'минуты', 'минут')}"]
        since = dt.datetime.fromtimestamp(psutil.boot_time()).strftime("%H:%M")
        return Reply(f"Компьютер работает {' '.join(parts)}, включён в {since}.")

    async def _do_battery(self, s) -> Reply:
        b = psutil.sensors_battery()
        if b is None:
            return Reply("Батареи нет — это настольный компьютер.")
        state = "заряжается" if b.power_plugged else "от батареи"
        return Reply(f"Заряд {round(b.percent)}%, {state}.")

    async def _do_ip(self, s) -> Reply:
        def local_ip():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))  # UDP connect sends nothing; picks the real default route
                return sock.getsockname()[0]
        try:
            ip = await run_blocking(local_ip)
        except OSError:
            ip = socket.gethostbyname(socket.gethostname())
        self.app.ui.show_deck(Deck(title="IP-адрес", cards=[Card("", ip, "")], done=True))
        return Reply(f"Локальный адрес {ip.replace('.', ' точка ')}.", tool_result=ip)

    async def _do_mac(self, s) -> Reply:
        import uuid

        mac = ":".join(f"{(uuid.getnode() >> i) & 0xFF:02X}" for i in range(40, -1, -8))
        self.app.ui.show_deck(Deck(title="MAC-адрес", cards=[Card("", mac, "")], done=True))
        return Reply("MAC-адрес на экране.", tool_result=mac)

    def _folder(self, spoken: str):
        folder = fsaccess.resolve_dir(self.app.cfg, spoken or "загрузки")
        if folder is None:
            return None, Reply(f"Папку «{spoken}» не нашёл среди разрешённых. Разрешённые папки меняются в настройках.",
                               tool_result="папка не разрешена или не найдена")
        return folder, None

    async def _do_count(self, s) -> Reply:
        folder, err = self._folder(s.get("dir", ""))
        if err:
            return err
        exts = fsaccess.kind_exts(s.get("kind", "") or "файл")
        n = await run_blocking(fsaccess.count, folder, exts)
        what = s.get("kind") or "файлов"
        where = fsaccess.spoken_place(folder)
        text = f"{where}: {n} — {what}."
        noun = plural(n, "файл", "файла", "файлов") if what.startswith("файл") else what
        if n == 0:
            return Reply(f"{where[:1].upper() + where[1:]} {noun} нет.", tool_result=text)
        return Reply(f"{where[:1].upper() + where[1:]} {n} {noun}.", tool_result=text)

    async def _do_recent(self, s) -> Reply:
        folder, err = self._folder(s.get("dir", ""))
        if err:
            return err
        items = await run_blocking(fsaccess.recent, folder, 8)
        if not items:
            return Reply(f"Папка {fsaccess.display_name(folder)} пуста.")
        self.found = items
        lines = [f"- {f.path.name}  ·  {fsaccess.human_size(f.size)}  ·  {dt.datetime.fromtimestamp(f.mtime):%d.%m %H:%M}"
                 for f in items]
        name = fsaccess.display_name(folder)
        self.app.ui.show_deck(Deck(title=f"Последнее: {name}", cards=[Card("", "\n".join(lines), "")], done=True, pinned=True))
        head = ", ".join(f.path.stem for f in items[:3])
        return Reply(f"Последнее {fsaccess.spoken_place(folder)}: {head}. Список на экране.",
                     tool_result="\n".join(f.path.name for f in items))

    async def _do_size(self, s) -> Reply:
        folder, err = self._folder(s.get("dir", ""))
        if err:
            return err
        total, files = await run_blocking(fsaccess.folder_size, folder)
        return Reply(f"Папка {fsaccess.display_name(folder)} весит {fsaccess.human_size(total)}, "
                     f"{files} {plural(files, 'файл', 'файла', 'файлов')}.")

    async def _do_find(self, s) -> Reply:
        name = s.get("name", "").strip()
        if not name:
            return Reply("Какой файл найти?")
        items = await run_blocking(fsaccess.find, self.app.cfg, name, 5)
        if not items:
            return Reply(f"Файл «{name}» в разрешённых папках не нашёл.", tool_result="не найдено")
        self.found = items
        lines = [f"{i + 1}. {f.path.name}\n   {f.path.parent}" for i, f in enumerate(items)]
        self.app.ui.show_deck(Deck(title=f"Поиск: {name}", cards=[Card("", "\n".join(lines), "")], done=True, pinned=True))
        first = items[0].path
        more = f" И ещё {len(items) - 1} на экране." if len(items) > 1 else ""
        return Reply(f"Нашёл {first.name} в папке {first.parent.name}.{more} Скажите «открой его».",
                     tool_result="\n".join(str(f.path) for f in items))

    async def _do_open_found(self, s) -> Reply:
        idx = int(s.get("index", 0))
        if idx >= len(self.found):
            return Reply("Такого файла в списке нет.")
        path = self.found[idx].path
        if not fsaccess.is_allowed(self.app.cfg, path):
            return Reply("Этот файл вне разрешённых папок.")
        await run_blocking(os.startfile, str(path))  # type: ignore[attr-defined]
        return Reply(f"Открываю {path.name}.", reaction="ok", listen_after=False)

    async def _do_open_dir(self, s) -> Reply:
        folder, err = self._folder(s.get("dir", ""))
        if err:
            return err
        await run_blocking(os.startfile, str(folder))  # type: ignore[attr-defined]
        return Reply(f"Открываю папку {fsaccess.display_name(folder)}.", reaction="ok", listen_after=False)


def create() -> Skill:
    return PcSkill()
