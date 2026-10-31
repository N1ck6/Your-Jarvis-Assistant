"""Prompt skills: a command of commands.toml that needs no code.

    phrase → where the text is (source) → prompt to a model (chain) → what to do with the answer (output)

[[command]]
action = "translate"
patterns = ['^переведи( это| выделенное)?$']
source = "selection"        # said | selection | screen | auto | previous | none
prompt = "translate"        # prompts/translate.md; {name} is replaced by a slot (see below)
chain = "local"             # local | info | vision | ["gemini", "groq"] | "@chain" (a value of [config])
output = "clipboard"        # speak | card | clipboard | replace | solution_answer | read
title = "Перевод · {lang}"

Slots for {name} in prompts, titles and replies: what the skill's match() put in the intent, what its prepare() added,
and "phrase" (what was said), "question" (the same), "where" (" с изображения" for a picture).
source = "previous" works again on the text or picture of this skill's last prompted command ("а если скорость 80?"
after "реши эту задачу"): slots "previous" (its answer), "task" (its phrase) and "changes" (every change asked since,
the intent's slot "change" is added to them).
SKILLS.md lists every field.
"""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any, AsyncIterator

from assistant import plaintext
from assistant.llm.hub import FAIL_TEXT
from assistant.llm.providers import Msg
from assistant.skills.base import Intent, Reply

if TYPE_CHECKING:
    from assistant.skillpack import Command
    from assistant.skills.base import Skill

log = logging.getLogger("prompting")

SOURCES = {"said", "selection", "screen", "auto", "previous", "none"}
OUTPUTS = {"speak", "card", "clipboard", "replace", "solution_answer", "read"}
OPTIONS = {"source", "prompt", "chain", "image_chain", "models", "local_fallback", "output", "title", "say_first",
           "empty_reply", "nothing", "nothing_reply", "fail_reply", "max_tokens", "local_warning", "speak_max",
           "speak_if", "recent_sec", "reaction", "listen_after", "web", "max_chars"}
DEFAULT_EMPTY = "Сначала выделите текст, а потом скажите, что с ним сделать."
NO_PREVIOUS = "Не помню прошлую задачу. Скажите её целиком или выделите."
LOCAL_NOTE = "Облако недоступно, ответил локально — лучше проверьте. "


def check(command: "Command") -> list[str]:
    """Problems of a prompt command (manifest check)."""
    o = command.options
    problems = [f"неизвестное поле «{k}»" for k in o if k not in OPTIONS]
    if o.get("source", "none") not in SOURCES:
        problems.append(f"source = «{o['source']}»: можно {sorted(SOURCES)}")
    if o.get("output", "speak") not in OUTPUTS:
        problems.append(f"output = «{o['output']}»: можно {sorted(OUTPUTS)}")
    if o.get("output") != "read" and "prompt" not in o and command.options:
        problems.append("нет prompt (файл prompts/<имя>.md)")
    return problems


def fill(template: str, slots: dict[str, Any]) -> str:
    """'{lang}' -> slots["lang"]; unknown names stay as they are."""
    return re.sub(r"\{(\w+)\}", lambda m: str(slots[m.group(1)]) if m.group(1) in slots else m.group(0), template)


class Runner:
    def __init__(self, skill: "Skill", command: "Command", intent: Intent) -> None:
        self.skill = skill
        self.ctx = skill.ctx
        self.o = command.options
        self.reply = command.reply
        self.intent = intent
        self.slots: dict[str, Any] = {k: v for k, v in intent.slots.items() if isinstance(v, (str, int, float, bool))}
        self.slots.setdefault("phrase", intent.raw or intent.text)
        self.slots.setdefault("question", intent.raw or intent.text)

    def opt(self, name: str, default: Any = None) -> Any:
        """A field of the command; "@name" takes the value from the skill's [config]."""
        value = self.o.get(name, default)
        if isinstance(value, str) and value.startswith("@"):
            return getattr(self.ctx.config, value[1:])
        return value

    def chain(self, images: bool) -> list[str]:
        spec = self.opt("image_chain") if images and self.o.get("image_chain") else self.opt("chain", "local")
        llm = self.ctx.settings.llm
        if isinstance(spec, str):
            spec = {"local": ["local"], "info": llm.info_chain, "vision": llm.vision_chain}.get(spec, [spec])
        chain = list(spec)
        if images:
            chain = [p for p in chain if p != "groq"]  # no vision on the Groq free tier
        if self.opt("local_fallback") is not None:     # the local model only when allowed, and only as the last one
            chain = [p for p in chain if p != "local"] + (["local"] if self.opt("local_fallback") else [])
        return chain

    async def run(self) -> Reply:
        source = self.opt("source", "none")
        text, images = "", []
        last = getattr(self.skill, "last_run", None)
        if source == "previous":
            if not last:
                return Reply(fill(self.opt("empty_reply", NO_PREVIOUS), self.slots))
            change = str(self.intent.slots.get("change") or "").strip()
            if change:
                last["changes"].append(change)
            text, images = last["text"], list(last["images"])
            self.slots.update(previous=last["answer"], task=last["phrase"],
                              changes="; ".join(last["changes"]) or "нет")
        elif source == "said":
            text = self.intent.raw or self.intent.text
        elif source == "auto":
            recent = self.ctx.recent("screen.region", float(self.opt("recent_sec", 300)))
            if recent is not None:
                images = [recent]
            else:
                text = await self.ctx.selection()
        elif source == "selection":
            text = await self.ctx.selection()
            if not text:
                return Reply(fill(self.opt("empty_reply", DEFAULT_EMPTY), self.slots))
        if source in ("screen", "auto") and not text and not images:
            png = await self.ctx.select_region()
            if not png:
                return Reply(listen_after=False)
            images = [png]
        text = text[: int(self.opt("max_chars", 6000))]
        if source != "previous":
            # what "а если …" works on next time; the answer is added when it comes
            self.skill.last_run = {"phrase": self.intent.raw or self.intent.text, "text": text, "images": images,
                                   "answer": "", "changes": [], "output": self.opt("output", "speak"),
                                   "at": time.monotonic()}
        self.slots["text"] = text
        self.slots["where"] = " с изображения" if images else ""
        self.slots.update(self.skill.prepare(self.intent, text) or {})
        log.info("%s.%s: %s", self.skill.name, self.intent.action,
                 "картинка" if images else f"текст, {len(text)} символов" if text else "фраза")

        output = self.opt("output", "speak")
        if output == "read":
            return Reply(stream=_once(text))
        messages = self.messages(text, images)
        chain = self.chain(bool(images))
        max_tokens = self.opt("max_tokens")
        models = {"gemini": self.opt("models")} if self.opt("models") else None
        if output == "speak":
            return Reply(stream=self._speak(messages, chain, max_tokens, models))
        if output == "replace" or not self.opt("say_first"):
            return await self.finish(await self.ask(messages, chain, max_tokens, models))
        return Reply(stream=self._finish_streamed(messages, chain, max_tokens, models))

    def messages(self, text: str, images: list[bytes]) -> list[Msg]:
        prompt = fill(self.ctx.prompt(self.opt("prompt")), self.slots)
        if images:
            return [Msg("user", prompt, images=images)]
        if text:
            return [Msg("system", prompt), Msg("user", text)]
        return [Msg("user", prompt)]

    async def ask(self, messages: list[Msg], chain: list[str], max_tokens: int | None, models) -> str:
        return (await self.ctx.ask(messages, chain=chain, web=bool(self.opt("web", False)), max_tokens=max_tokens,
                                   models=models)).strip()

    async def _speak(self, messages, chain, max_tokens, models) -> AsyncIterator[str]:
        if self.opt("say_first"):
            yield fill(self.opt("say_first"), self.slots)
        async for delta in self.ctx.stream(messages, chain=chain, web=bool(self.opt("web", False)),
                                           max_tokens=max_tokens, models=models):
            yield delta

    async def _finish_streamed(self, messages, chain, max_tokens, models) -> AsyncIterator[str]:
        """"Решаю." at once, the result when the model is done."""
        yield fill(self.opt("say_first"), self.slots)
        reply = await self.finish(await self.ask(messages, chain, max_tokens, models))
        if reply.speech:
            yield reply.speech

    async def finish(self, answer: str) -> Reply:
        """What to do with the model's answer."""
        if not answer or answer == FAIL_TEXT or answer.startswith("Не получилось"):
            return Reply(fill(self.opt("fail_reply", FAIL_TEXT), self.slots))
        last = getattr(self.skill, "last_run", None)
        if last is not None and last["output"] == self.opt("output", "speak"):
            last["answer"], last["at"] = answer, time.monotonic()  # a new solution replaces the old one
        nothing = self.opt("nothing")
        if nothing and answer.upper().startswith(nothing.upper()) and len(answer) < len(nothing) + 30:
            return Reply(fill(self.opt("nothing_reply", "Здесь ничего не нашёл."), self.slots), listen_after=False)
        local = (self.opt("local_warning", False) and self.ctx.last_provider == "local")
        note = LOCAL_NOTE if local else ""
        title = fill(str(self.opt("title", self.skill.title)), self.slots)
        listen_after = bool(self.opt("listen_after", True))
        reaction = str(self.opt("reaction", ""))
        output = self.opt("output", "speak")

        if output == "replace":
            await self.ctx.paste(answer)  # the selection is still active: replaced in place
            return Reply(fill(self.reply or "Готово.", self.slots), reaction=reaction or "ok", listen_after=False)
        if output == "solution_answer":
            solution, short = plaintext.split_solution(answer)
            if solution:
                self.ctx.card("Решение", solution)
            if short:
                self.ctx.card("Ответ · локально, может быть неточно" if local else "Ответ", short)
            speech = ("Ответ: " + plaintext.spoken(short)) if short and len(short) <= 120 else "Решение и ответ на экране."
            log.info("Решено (%s): ответ %d символов", self.ctx.last_provider, len(short))
            return Reply(note + speech, listen_after=listen_after)
        if output == "clipboard":
            await self.ctx.set_clipboard(answer)
        self.ctx.card(title + (" · локально, может быть неточно" if local else ""), answer)
        speak_if = self.o.get("speak_if")
        if (not speak_if or self.slots.get(speak_if)) and len(answer) <= int(self.opt("speak_max", 0)):
            return Reply(note + answer, tool_result=answer[:500], listen_after=listen_after)
        default = "Скопировал в буфер обмена." if output == "clipboard" else "Показал на экране."
        return Reply(note + fill(self.reply or default, self.slots), reaction=reaction, tool_result=answer[:500],
                     listen_after=listen_after)


async def run(skill: "Skill", command: "Command", intent: Intent) -> Reply:
    return await Runner(skill, command, intent).run()


async def _once(text: str) -> AsyncIterator[str]:
    yield text
