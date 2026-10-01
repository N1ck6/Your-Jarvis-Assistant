"""Notes and lists as Markdown files (data/notes/<list>.md, or an Obsidian vault via [notes] dir).

"добавь в список покупок молоко и хлеб", "что в списке покупок", "вычеркни молоко", "молоко купил",
"запиши в заметки ...", "какие у меня дела", "очисти список покупок" (asks for confirmation).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

from rapidfuzz import fuzz

from assistant.core import Card, Deck
from assistant.nlu import plural
from assistant.skills.base import Intent, Reply, Skill, Tool

log = logging.getLogger("notes")

# spoken list name -> file name
_LISTS = [
    (re.compile(r"покуп\w*|магазин\w*|продукт\w*|что купить"), "покупки"),
    (re.compile(r"дел[ао]?\b|дел$|задач\w*|туду|todo|to do|планы на день"), "дела"),
    (re.compile(r"заметк\w*|записк\w*"), "заметки"),
    (re.compile(r"фильм\w*|кино"), "фильмы"),
    (re.compile(r"книг\w*|почитать"), "книги"),
    (re.compile(r"иде[яий]\w*"), "идеи"),
]
_ADD_VERB = r"(добавь|добавить|запиши|записать|внеси|внести|занеси|занести|пометь|закинь|кинь|положи|впиши|вставь|сохрани|допиши)"
_LIST_NAME = r"(?:список\s+|списке\s+|лист\s+)?(?P<list>[а-яa-z -]+?)"
_ADD_TO = re.compile(rf"^{_ADD_VERB}\s+(?:в|во|к)\s+{_LIST_NAME}\s+(?P<items>.+)$")
_ADD_TO_REV = re.compile(rf"^{_ADD_VERB}\s+(?P<items>.+?)\s+(?:в|во)\s+(?:список\s+|списке\s+)?(?P<list>покуп\w*|дел[ао]?|задач\w*|заметк\w*|фильм\w*|книг\w*|иде\w*)$")
_NOTE = re.compile(r"^(сделай заметку|создай заметку|заметка|запиши заметку|новая заметка|запомни в заметках|"
                   r"запиши себе|запиши мне|запиши|зафиксируй|пометь себе)\s+(?P<items>.+)$")
_BUY = re.compile(r"^(надо|нужно|не забыть|не забудь)?\s*(купить|докупить|взять в магазине)\s+(?P<items>.+)$")
_TODO = re.compile(r"^(добавь задачу|добавь дело|новая задача|новое дело|задача|запиши задачу|запиши дело)\s+(?P<items>.+)$")
_READ = re.compile(
    r"^(что|чего) (у меня |еще |мне )?(в|во|на) (списке |список )?(?P<list>.+)$|"
    r"^(прочитай|прочти|покажи|открой|озвучь|скажи|зачитай|выведи|перечисли|напомни|дай)( мне)?( мой| мои| весь)?( список| списки)? (?P<list2>.+)$|"
    r"^какие (у меня )?(?P<list3>дела|задачи|заметки|покупки|планы)( есть| на сегодня| сегодня)?$|"
    r"^что (мне )?(нужно|надо) (?P<list4>купить|сделать)( сегодня)?$|^(мои )?(?P<list5>дела|задачи|заметки|покупки)$"
)
_REMOVE = re.compile(
    rf"^(удали|убери|вычеркни|сотри|вычеркнуть|удалить|отметь|отметить|зачеркни)\s+(?:из\s+{_LIST_NAME}\s+)?(?P<item>.+?)"
    rf"(?:\s+из\s+(?:списка\s+)?(?P<list2>покуп\w*|дел|задач\w*|заметок|фильмов|книг))?(?:\s+как\s+(?:сделанное|купленное|выполненное))?$"
)
_DONE = re.compile(r"^(?P<item>.+?)\s+(купил|купила|куплено|куплен|купили|сделал|сделала|сделано|готово|выполнил|выполнено)$")
_CLEAR = re.compile(rf"^(очисти|очистить|удали все|удали всё|сотри все|обнули|почисти|удалить все|сбрось)\s+(из\s+)?(весь\s+)?{_LIST_NAME}$")
_SPLIT = re.compile(r"\s*(?:,|\bи еще\b|\bи\b|\bа также\b|\bплюс\b|\bа еще\b)\s*")
_RAW_TRIGGER = re.compile(
    r"^\W*(?:сделай заметку|создай заметку|запиши заметку|новая заметка|запомни в заметках|заметка|запиши себе|запиши мне|"
    r"запиши в заметки|добавь в заметки|внеси в заметки|запиши|зафиксируй|пометь себе)\W+", re.I)


def split_items(items_norm: str, raw: str) -> list[str]:
    """Commas are gone from the normalized command; recover them from the raw phrase."""
    lite = re.sub(r"[^\w\s,-]", " ", raw.lower().replace("ё", "е"))
    lite = re.sub(r"\s+", " ", lite)
    words = items_norm.split()
    source = items_norm
    if words:
        m = re.search(r"\b" + r"[\s,]+".join(map(re.escape, words)) + r"\b", lite)
        if m:
            source = m.group(0)
    return [i.strip(" ,") for i in _SPLIT.split(source) if i.strip(" ,")]


def list_file_name(spoken: str) -> str | None:
    spoken = spoken.strip()
    for pattern, name in _LISTS:
        if pattern.search(spoken):
            return name
    return None


class NotesSkill(Skill):
    name = "notes"
    title = "Заметки и списки"
    examples = [
        'добавь в список покупок <что>',
        'добавь в список дел <что>',
        'запиши <заметка>',
        'что в списке покупок',
        'какие у меня дела',
        'вычеркни <пункт>',
        'очисти список покупок',
    ]

    def folder(self) -> Path:
        folder = self.app.cfg.resolve(self.app.cfg.notes.dir)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    # ------------------------------------------------------------------ storage
    def _path(self, name: str) -> Path:
        return self.folder() / f"{name}.md"

    def items(self, name: str) -> list[str]:
        path = self._path(name)
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.*\S)", line)
            if m and not re.match(r"^\s*[-*]\s+\[[xX]\]", line):
                out.append(m.group(1))
        return out

    def _write(self, name: str, items: list[str]) -> None:
        checkbox = name in ("покупки", "дела")
        body = "\n".join(f"- [ ] {i}" if checkbox else f"- {i}" for i in items)
        self._path(name).write_text(f"# {name.capitalize()}\n\n{body}\n" if items else f"# {name.capitalize()}\n",
                                    encoding="utf-8")

    def add(self, name: str, new: list[str]) -> list[str]:
        items = self.items(name)
        added = [i for i in new if i and all(fuzz.ratio(i.lower(), x.lower()) < 90 for x in items)]
        self._write(name, items + added)
        return added

    def remove(self, name: str, spoken: str) -> str | None:
        items = self.items(name)
        best = max(items, key=lambda i: fuzz.WRatio(spoken, i.lower()), default=None)
        if best is None or fuzz.WRatio(spoken, best.lower()) < 75:
            return None
        items.remove(best)
        self._write(name, items)
        return best

    # ------------------------------------------------------------------ matching
    def match(self, text: str) -> Intent | None:
        default = self.app.cfg.notes.default_list
        if m := _CLEAR.match(text):
            if name := list_file_name(m.group("list")):
                return Intent(self.name, "clear", {"list": name}, text)
        for pattern in (_ADD_TO, _ADD_TO_REV):
            if m := pattern.match(text):
                if name := list_file_name(m.group("list")):
                    return Intent(self.name, "add", {"list": name, "items": m.group("items")}, text)
        if m := _BUY.match(text):
            return Intent(self.name, "add", {"list": "покупки", "items": m.group("items")}, text)
        if m := _TODO.match(text):
            return Intent(self.name, "add", {"list": "дела", "items": m.group("items")}, text)
        if m := _NOTE.match(text):
            if not re.match(r"^(текст|под диктовку)", m.group("items")):
                return Intent(self.name, "add", {"list": default, "items": m.group("items"), "free": True}, text)
        if m := _READ.match(text):
            spoken = next(v for k, v in m.groupdict().items() if v and k.startswith("list"))
            spoken = {"купить": "покупки", "сделать": "дела", "планы": "дела"}.get(spoken, spoken)
            if name := list_file_name(spoken):
                return Intent(self.name, "read", {"list": name}, text)
        if m := _REMOVE.match(text):
            spoken_list = m.group("list") or m.group("list2") or ""
            if len(m.group("item")) < 3:
                return None
            name = list_file_name(spoken_list) if spoken_list else self._find_list_with(m.group("item"))
            if name:
                return Intent(self.name, "remove", {"list": name, "item": m.group("item")}, text)
        if (m := _DONE.match(text)) and len(m.group("item")) >= 3:
            name = self._find_list_with(m.group("item"))
            if name:
                return Intent(self.name, "remove", {"list": name, "item": m.group("item")}, text)
        return None

    def _find_list_with(self, spoken: str) -> str | None:
        for name in ("покупки", "дела", self.app.cfg.notes.default_list):
            if any(fuzz.ratio(spoken, i.lower()) >= 80 or (len(spoken) >= 4 and spoken in i.lower()) for i in self.items(name)):
                return name
        return None

    def tools(self) -> list[Tool]:
        return [
            Tool("notes_add", "Добавить пункты в список (покупки, дела, заметки, фильмы, книги, идеи).",
                 {"type": "object", "properties": {"list": {"type": "string"}, "items": {"type": "string"}},
                  "required": ["list", "items"]}, "add"),
            Tool("notes_read", "Прочитать список пользователя.",
                 {"type": "object", "properties": {"list": {"type": "string"}}, "required": ["list"]}, "read"),
        ]

    # ------------------------------------------------------------------ actions
    async def handle(self, intent: Intent) -> Reply:
        name = list_file_name(str(intent.slots.get("list", ""))) or str(intent.slots.get("list") or self.app.cfg.notes.default_list)
        if intent.action == "add":
            if intent.slots.get("free"):
                raw = _RAW_TRIGGER.sub("", intent.raw).strip() if intent.raw else intent.slots["items"]
                stamp = dt.datetime.now().strftime("%d.%m %H:%M")
                self.add(name, [f"{stamp} — {raw[:1].upper() + raw[1:]}"])
                return Reply("Записал.", reaction="ok", listen_after=False)
            new = split_items(str(intent.slots["items"]), intent.raw or str(intent.slots["items"]))
            added = self.add(name, new)
            if not added:
                return Reply(f"Это уже есть в списке «{name}».")
            return Reply(f"Добавил в «{name}»: {', '.join(added)}.", listen_after=False)
        if intent.action == "read":
            items = self.items(name)
            if not items:
                return Reply(f"Список «{name}» пуст.")
            self.app.ui.show_deck(Deck(title=name.capitalize(), cards=[Card("", "\n".join(f"- {i}" for i in items), "")],
                                       done=True))
            n = len(items)
            head = ", ".join(items[:7])
            more = f" и ещё {n - 7}" if n > 7 else ""
            return Reply(f"В списке «{name}» {n} {plural(n, 'пункт', 'пункта', 'пунктов')}: {head}{more}.")
        if intent.action == "remove":
            removed = self.remove(name, str(intent.slots["item"]))
            if not removed:
                return Reply(f"Не нашёл «{intent.slots['item']}» в списке «{name}».")
            return Reply(f"Вычеркнул {removed}.", listen_after=False)
        if intent.action == "clear":
            n = len(self.items(name))
            if not n:
                return Reply(f"Список «{name}» и так пуст.")

            async def do() -> Reply:
                self._write(name, [])
                return Reply(f"Список «{name}» очищен.", listen_after=False)

            return Reply(f"Очистить список «{name}», {n} {plural(n, 'пункт', 'пункта', 'пунктов')}?", confirm=do)
        return Reply()


def create() -> Skill:
    return NotesSkill()
