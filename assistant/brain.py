"""Request routing: skill fast path -> cloud (facts / internet) -> local model with tools."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections import deque
from typing import TYPE_CHECKING, AsyncIterator

from assistant.llm.providers import LocalTurn, Msg
from assistant.nlu import NO, YES, normalize_command
from assistant.skills.base import Intent, Reply, Skill, Tool

if TYPE_CHECKING:
    from assistant.assistant import Assistant

log = logging.getLogger("brain")

_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

BASE_PROMPT = """Ты — {name}, личный ассистент пользователя на его компьютере, как J.A.R.V.I.S. у Тони Старка:
собранный, точный, спокойный, с лёгкой сухой иронией, всегда на стороне хозяина. Ответы озвучиваются голосом.
Главное — скорость и польза: сразу давай ровно то, что нужно, 1–3 коротких предложения, без вступлений и воды.
Иногда обращайся к пользователю «{address}», но не в каждой фразе. Говори по-русски, в мужском роде.
Никакого markdown, списков, эмодзи, ссылок и источников. Если не знаешь — так и скажи.
Сейчас {now}, {weekday}. Город пользователя по умолчанию: {city}.{facts}"""

INFO_ADDON = """Если вопрос про факты, новости, цены, курсы, события или что-то свежее — найди в интернете.
Дай сам ответ сразу, без вступлений вроде «Согласно источникам»."""

LOCAL_ADDON = """У тебя есть инструменты для действий на компьютере. Если пользователь просит что-то сделать —
вызови подходящий инструмент, а не описывай действие словами. Никогда не выдумывай результат действия.
Если нужен свежий факт из интернета — вызови ask_internet."""

# Questions that need facts or fresh data go to the cloud.
_INFO = re.compile(
    r"^(что такое|кто так(ой|ая|ие)|кто (был|была|написал|изобрел|изобрёл|создал|сейчас)|что значит|что означает|"
    r"когда|где|сколько|какой|какая|какое|какие|каков|почему|зачем|чем|откуда|правда ли|есть ли|"
    r"найди|узнай|поищи|загугли|посмотри в интернете)"
    r"|новост|курс (доллара|евро|рубля|биткоина)|сколько стоит|цена|счёт матча|счет матча|кто выиграл|последн(ие|яя|ий)"
)
# Small talk stays local even if it looks like a question.
_SMALLTALK = re.compile(
    r"^(как (у тебя )?дела|как ты|как настроение|как жизнь|привет|здравствуй|доброе утро|добрый (день|вечер)|спасибо|"
    r"пока|расскажи (шутку|анекдот|что-нибудь)|ты кто|как тебя зовут|что делаешь|чем занят)"
)
CONFIRM_TTL_SEC = 20

ASK_INTERNET = Tool("ask_internet", "Найти свежую информацию в интернете и ответить на фактический вопрос.",
                    {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
                    "ask")


class Dialog:
    """Short rolling context, forgotten after a pause."""

    def __init__(self, ttl_sec: float, max_turns: int) -> None:
        self.ttl = ttl_sec
        # (time, role, content, is_action). Action turns are hidden from the LLM: a history of
        # "set a timer" -> "Timer set" without tool calls teaches the model to fake actions.
        self.turns: deque[tuple[float, str, str, bool]] = deque(maxlen=max_turns * 2)

    def _expire(self) -> None:
        if self.turns and time.monotonic() - self.turns[-1][0] > self.ttl:
            self.turns.clear()

    def add(self, role: str, content: str, action: bool = False) -> None:
        self._expire()
        if content:
            self.turns.append((time.monotonic(), role, content, action))

    def mark_last_user_action(self) -> None:
        for i in range(len(self.turns) - 1, -1, -1):
            t, role, content, _ = self.turns[i]
            if role == "user":
                self.turns[i] = (t, role, content, True)
                return

    def messages(self) -> list[Msg]:
        self._expire()
        return [Msg(role, content) for _, role, content, action in self.turns if not action]

    def last_topic(self) -> str:
        self._expire()
        users = [c for _, r, c, _ in self.turns if r == "user"]
        return users[-2] if len(users) >= 2 else ""


def is_info_question(text: str) -> bool:
    low = normalize_command(text)
    return not _SMALLTALK.search(low) and bool(_INFO.search(low))


class Brain:
    def __init__(self, app: "Assistant", skills: list[Skill]) -> None:
        self.app = app
        self.skills = skills
        # Exclusive skill calls requested by the local model, run after its speech ends.
        self.deferred: list[tuple[Skill, Intent]] = []
        self._turn_used_tools = False
        # A dangerous action waiting for "да": (callback, deadline).
        self.pending: tuple | None = None
        self.tools: dict[str, tuple[Skill | None, Tool]] = {ASK_INTERNET.name: (None, ASK_INTERNET)}
        for skill in skills:
            for tool in skill.tools():
                self.tools[tool.name] = (skill, tool)

    def _system(self, addon: str) -> Msg:
        cfg = self.app.cfg.assistant
        now = dt.datetime.now()
        facts = self.user_facts()
        facts_text = ("\nЧто ты знаешь о пользователе (учитывай, но не пересказывай без повода):\n- " + "\n- ".join(facts)
                      if facts else "")
        return Msg("system", BASE_PROMPT.format(name=cfg.name, address=cfg.address, now=now.strftime("%d.%m.%Y %H:%M"),
                                                weekday=_WEEKDAYS[now.weekday()], city=cfg.default_city,
                                                facts=facts_text) + "\n" + addon)

    def user_facts(self) -> list[str]:
        memory = next((s for s in self.skills if s.name == "memory"), None)
        return memory.facts() if memory is not None else []  # type: ignore[attr-defined]

    def match_skill(self, text: str) -> tuple[Skill, Intent] | None:
        norm = normalize_command(text)
        for skill in self.skills:
            try:
                intent = skill.match(norm)
            except Exception:
                log.exception("match() в модуле %s", skill.name)
                continue
            if intent:
                intent.text = intent.text or norm
                intent.raw = text
                return skill, intent
        return None

    def _remember_confirm(self, reply: Reply) -> None:
        if reply.confirm is not None:
            self.pending = (reply.confirm, time.monotonic() + CONFIRM_TTL_SEC)

    async def _resolve_pending(self, text: str) -> Reply | None:
        """Answers a pending "are you sure?" question; None if the phrase is something else."""
        if self.pending is None:
            return None
        action, deadline = self.pending
        self.pending = None
        if time.monotonic() > deadline:
            return None
        norm = normalize_command(text, strip_polite=False)
        if YES.match(norm):
            log.info("Подтверждено")
            return await action()
        if NO.match(norm):
            return Reply("Отменил.", listen_after=False)
        return None

    async def handle(self, text: str) -> Reply:
        """Returns a Reply; streaming replies must be spoken by the caller."""
        confirmed = await self._resolve_pending(text)
        if confirmed is not None:
            return confirmed
        hit = self.match_skill(text)
        if hit:
            skill, intent = hit
            log.info("Модуль %s.%s %s", skill.name, intent.action, intent.slots or "")
            self.app.dialog.add("user", text, action=True)
            reply = await skill.handle(intent)
            self._remember_confirm(reply)
            if reply.speech:
                self.app.dialog.add("assistant", reply.speech, action=True)
            return reply

        history = self.app.dialog.messages()
        self.app.dialog.add("user", text)
        if is_info_question(text):
            log.info("Маршрут: облако (вопрос с фактами)")
            messages = [self._system(INFO_ADDON), *history, Msg("user", text)]
            return Reply(stream=self._record(self.app.llm.stream(self.app.cfg.llm.info_chain, messages, web=True)))
        log.info("Маршрут: локальная модель")
        return Reply(stream=self._record(self._local_agent(history, text)))

    async def _record(self, stream: AsyncIterator[str]) -> AsyncIterator[str]:
        parts: list[str] = []
        self._turn_used_tools = False
        try:
            async for delta in stream:
                parts.append(delta)
                yield delta
        finally:
            if self._turn_used_tools:
                self.app.dialog.mark_last_user_action()
            self.app.dialog.add("assistant", "".join(parts).strip(), action=self._turn_used_tools)

    async def _local_agent(self, history: list[Msg], text: str) -> AsyncIterator[str]:
        """Local model with tool calling. Direct tools answer the user themselves."""
        local = self.app.llm.local
        self.app.llm.last_provider = "local"
        messages = [self._system(LOCAL_ADDON), *history, Msg("user", text)]
        schemas = [tool.schema() for _, tool in self.tools.values()]
        for _round in range(3):
            turn = LocalTurn()
            try:
                async for delta in local.stream_with_tools(messages, schemas, turn, max_tokens=500):
                    yield delta
            except Exception as exc:
                log.warning("Локальная модель: %s", exc)
                async for delta in self.app.llm.stream(self.app.cfg.llm.info_chain, messages, web=False):
                    yield delta
                return
            if not turn.tool_calls:
                return
            self._turn_used_tools = True
            messages.append(Msg("assistant", turn.text, tool_calls=turn.tool_calls))
            direct_done = False
            for call in turn.tool_calls:
                name = call["function"]["name"]
                args = call["function"].get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                log.info("Инструмент %s %s", name, args)
                if name == ASK_INTERNET.name:
                    q = str(args.get("question") or text)
                    ask = [self._system(INFO_ADDON), Msg("user", q)]
                    async for delta in self.app.llm.stream(self.app.cfg.llm.info_chain, ask, web=True):
                        yield delta
                    direct_done = True
                    continue
                if name not in self.tools:
                    messages.append(Msg("tool", f"Нет инструмента {name}", tool_name=name))
                    continue
                skill, tool = self.tools[name]
                assert skill is not None
                intent = Intent(skill.name, tool.action, dict(args), text)
                if tool.exclusive:
                    self.deferred.append((skill, intent))
                    direct_done = True
                    continue
                intent.text = normalize_command(text)
                intent.raw = text
                reply = await skill.handle(intent)
                self._remember_confirm(reply)
                if reply.speech:
                    yield (" " if turn.text else "") + reply.speech
                if reply.stream:
                    async for delta in reply.stream:
                        yield delta
                messages.append(Msg("tool", reply.tool_result or reply.speech or "готово", tool_name=name))
                direct_done = direct_done or tool.direct
            if direct_done:
                return
