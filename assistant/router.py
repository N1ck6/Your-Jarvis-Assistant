"""Fallback command router: an LLM turns a free-form or context-dependent request into Jarvis commands.

Order in the brain: regex modules -> compound split -> learned phrasebook -> this router -> answer.
The router sees a compact command catalog and the last few turns and answers with JSON:
  {"kind": "commands", "commands": ["громче", "включи музыку"], "context": false}
  {"kind": "web", "query": "<self-contained question>", "fresh": true}
  {"kind": "chat", "query": "<self-contained question>"}
Successful context-free mappings are remembered in data/phrasebook.json and never cost a model call again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from assistant.llm.providers import Msg
from assistant.log import private
from assistant.paths import DATA_DIR

if TYPE_CHECKING:
    from assistant.assistant import Assistant

log = logging.getLogger("router")
PHRASEBOOK = DATA_DIR / "phrasebook.json"
ANSWERS = DATA_DIR / "answer_cache.json"

PROMPT = """Ты — маршрутизатор голосового ассистента. Выбери один вариант:
1) "commands" — запрос выполним командами из списка. Верни команды в точной форме из списка с подставленными
   значениями (несколько — по порядку). Сюда же: погода и как одеться, диск и память компьютера, музыка, таймеры,
   дела и покупки. Короткое уточнение к прошлой команде («а завтра?», «ещё громче», «закрой его») — та же команда
   с новыми значениями и context = true.
2) "web" — нужен точный или свежий факт: люди, даты, числа, события, новости, курсы, цены, счёт матчей, расписания.
3) "chat" — объяснение понятий, совет, мнение, разговор, шутка.
4) "ignore" — обрывок, бессмыслица, одно невнятное слово или фраза явно не ассистенту (разговор с другим человеком,
   звук телевизора). Никогда не угадывай команду по невнятному слову.
Для "web" и "chat" в query перепиши запрос самодостаточно, с учётом прошлых реплик (замени «он», «это» и т. п.).

Команды:
{catalog}

Ответь ОДНОЙ строкой в одном из форматов:
К: <команда>; <команда>   — команды из списка (К+: если запрос непонятен без прошлых реплик)
W: <вопрос>               — нужен свежий факт из интернета
F: <вопрос>               — факт, который не меняется со временем
C: <запрос>               — разговор, объяснение, совет
I                         — игнорировать
Обычно нужна ровно одна команда; несколько — только если пользователь сам просит несколько действий.
Жалоба «не слышно», «тихо» — это громче; «не дай забыть через N минут» — таймер с напоминанием;
место на компе — это диск, а оперативка — это память; «что я выделил» — команды с выделенным текстом."""

# One short line instead of JSON: ~5 output tokens instead of ~35, the answer comes in ~0.4 s instead of ~1.1 s
# on qwen3:8b with the same accuracy (scripts/router_bench.py). JSON answers are still understood.
_LINE = re.compile(r"^\s*(К\+|K\+|К|K|W|F|C|С|I)\s*:?\s*(.*)$")


@dataclass
class Decision:
    kind: str                       # commands | web | chat | ignore
    commands: list[str] = field(default_factory=list)
    query: str = ""
    fresh: bool = True
    context: bool = False
    source: str = "model"           # model | phrasebook | heuristic


@dataclass
class Turn:
    text: str       # what the user said
    did: str        # what Jarvis did / answered (short)


def _parse(raw: str) -> dict | None:
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    if m := _LINE.match(line):
        tag, rest = m.group(1).upper().replace("К", "K").replace("С", "C"), m.group(2).strip()
        if tag.startswith("K"):
            return {"kind": "commands", "commands": [c.strip(" .") for c in rest.split(";") if c.strip(" .")],
                    "context": tag == "K+"}
        if tag in ("W", "F"):
            return {"kind": "web", "query": rest, "fresh": tag == "W"}
        if tag == "C":
            return {"kind": "chat", "query": rest}
        return {"kind": "ignore"}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


class _JsonStore:
    def __init__(self, path, limit: int) -> None:
        self.path = path
        self.limit = limit
        self.data: dict[str, dict] = {}
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        if len(self.data) > self.limit:  # keep the most recently used
            items = sorted(self.data.items(), key=lambda kv: kv[1].get("ts", 0))
            self.data = dict(items[-self.limit:])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")


class Router:
    def __init__(self, app: "Assistant") -> None:
        self.app = app
        self.phrasebook = _JsonStore(PHRASEBOOK, 1000)
        self.answers = _JsonStore(ANSWERS, 500)
        self._catalog = ""
        self._candidates: dict[str, list[str]] = {}
        self._model_missing = ""   # the configured router model is not installed: use the main one

    # ------------------------------------------------------------------ catalog
    def catalog(self) -> str:
        if not self._catalog:
            lines: list[str] = []
            for skill in self.app.skills:
                examples = getattr(skill, "examples", [])
                if callable(examples):
                    examples = examples()
                if examples:
                    lines.append("- " + " | ".join(examples))
            self._catalog = "\n".join(lines)
        return self._catalog

    def reset_catalog(self) -> None:
        self._catalog = ""

    # ------------------------------------------------------------------ learned phrases
    def learned(self, norm: str) -> list[str] | None:
        item = self.phrasebook.data.get(norm)
        if item:
            item["ts"] = time.time()
            item["uses"] = item.get("uses", 0) + 1
            return list(item["commands"])
        return None

    def learn(self, norm: str, commands: list[str]) -> None:
        """Remembers a mapping once the model has given the same answer twice (one-off mistakes are not kept)."""
        if self._candidates.get(norm) != commands:
            self._candidates[norm] = commands
            return
        self._candidates.pop(norm, None)
        self.phrasebook.data[norm] = {"commands": commands, "ts": time.time(), "uses": 0}
        self.phrasebook.save()
        log.info("Запомнил формулировку «%s» → %s", private(norm), commands)

    def forget(self, norm: str) -> bool:
        if self.phrasebook.data.pop(norm, None) is not None:
            self.phrasebook.save()
            return True
        return False

    def drop_candidate(self, norm: str) -> None:
        """"не то": a mapping waiting for its second confirmation is dropped too."""
        self._candidates.pop(norm, None)

    def forget_answer(self, key: str) -> None:
        if self.answers.data.pop(key, None) is not None:
            self.answers.save()

    # ------------------------------------------------------------------ answer cache (saves API tokens)
    def cached_answer(self, query: str) -> str | None:
        item = self.answers.data.get(query)
        if item and time.time() - item["ts"] < item["ttl"]:
            return item["text"]
        return None

    def remember_answer(self, query: str, text: str, fresh: bool) -> None:
        if not text or len(text) < 5:
            return
        cfg = self.app.cfg.router
        ttl = cfg.cache_fresh_min * 60 if fresh else cfg.cache_stable_days * 86400
        self.answers.data[query] = {"text": text, "ts": time.time(), "ttl": ttl}
        self.answers.save()

    # ------------------------------------------------------------------ routing
    def _provider_chain(self) -> list[str]:
        mode = self.app.cfg.router.provider
        if mode == "local" or (mode == "auto" and self.app.llm.local.healthy):
            return ["local"]
        return [p for p in self.app.cfg.llm.info_chain if p != "local"] or ["local"]

    async def route(self, text: str, turns: list[Turn], addressed: bool = True) -> Decision | None:
        cfg = self.app.cfg.router
        if not cfg.enabled:
            return None
        history = "\n".join(f"- пользователь: «{t.text}» → {t.did}" for t in turns[-cfg.context_turns:]) or "- (нет)"
        how = ("Пользователь обратился по имени." if addressed else
               "Фраза прозвучала сразу после ответа ассистента БЕЗ обращения по имени: если она не похожа на "
               "продолжение разговора с ассистентом или на команду ему — kind \"ignore\".")
        messages = [Msg("system", PROMPT.format(catalog=self.catalog())),
                    Msg("user", f"Последние запросы:\n{history}\n\n{how}\nНовый запрос: «{text}»")]
        chain = self._provider_chain()
        t = time.perf_counter()
        try:
            if chain == ["local"]:
                model = cfg.model if cfg.model and cfg.model != self._model_missing else ""
                try:
                    raw = await asyncio.wait_for(self.app.llm.local.complete(messages, max_tokens=120, model=model),
                                                 timeout=cfg.timeout_sec)
                except Exception as exc:
                    if not model or "not found" not in str(exc).lower():
                        raise
                    log.warning("Модель маршрутизатора %s не скачана, использую основную", model)
                    self._model_missing = model
                    raw = await asyncio.wait_for(self.app.llm.local.complete(messages, max_tokens=120),
                                                 timeout=cfg.timeout_sec)
            else:
                raw = await asyncio.wait_for(self.app.llm.complete(chain, messages, web=False, max_tokens=160),
                                             timeout=cfg.timeout_sec)
        except Exception as exc:
            log.warning("Маршрутизатор недоступен: %s", exc)
            return None
        data = _parse(raw)
        log.info("Маршрутизатор %.2f с (%s): %s", time.perf_counter() - t, chain[0], private(raw.strip()[:200]))
        if not data or data.get("kind") not in ("commands", "web", "chat", "ignore"):
            return None
        commands = [str(c).strip() for c in data.get("commands") or [] if str(c).strip()][:4]
        kind = data["kind"]
        if kind == "commands" and not commands:
            kind = "chat"
        return Decision(kind, commands, str(data.get("query") or "").strip(), bool(data.get("fresh", True)),
                        bool(data.get("context", False)))
