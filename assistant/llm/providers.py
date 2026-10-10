"""LLM providers: Gemini (Google Search grounding), Groq (browser_search), local Ollama."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from assistant.config import CloudCfg, LocalLlmCfg, secret

log = logging.getLogger("llm")


SEARCH_BLOCK_SEC = 3600


class ProviderError(Exception):
    """Provider failed; the chain should try the next one."""


class QuotaError(ProviderError):
    """Rate limit / free quota exhausted; the provider is put on cooldown.

    retry_after: seconds the API asked to wait (None = unknown); daily: the daily quota is over."""

    def __init__(self, message: str, retry_after: float | None = None, daily: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.daily = daily


_WAIT = re.compile(r"(?:try again in|retry in|retrydelay\W+)\s*(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?", re.I)


def parse_wait(text: str) -> float | None:
    """'Please try again in 2m30.5s' / "'retryDelay': '17s'" -> seconds."""
    m = _WAIT.search(text)
    if not m or not any(m.groups()):
        return None
    h, mnt, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mnt * 60 + s


def _is_daily(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in ("per day", "perday", "(rpd)", "(tpd)", "daily"))


class AuthError(ProviderError):
    """Invalid or missing API key; retrying is pointless until the key is fixed."""


def _is_auth_problem(code: int, message: str) -> bool:
    msg = message.lower()
    return code in (401, 403) or "api key not valid" in msg or "api_key_invalid" in msg or "permission" in msg


@dataclass
class Msg:
    role: str      # "system" | "user" | "assistant" | "tool"
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_name: str = ""
    images: list[bytes] = field(default_factory=list)  # PNG/JPEG for vision models


class Provider(ABC):
    name = ""
    last_usage: tuple[int, int] = (0, 0)  # (input tokens, output tokens) of the last finished request

    @abstractmethod
    def available(self) -> bool: ...

    def can_search(self) -> bool:
        """Answers with fresh data from the internet right now."""
        return False

    @abstractmethod
    def stream(self, messages: list[Msg], *, web: bool, max_tokens: int | None = None,
               models: list[str] | None = None) -> AsyncIterator[str]:
        """Yields text deltas. Must raise before the first delta if the request fails.
        models: try these models instead of the configured ones (the browser agent plans with Gemini Flash)."""


# --------------------------------------------------------------------------- Gemini
class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, cfg: CloudCfg, timeout_sec: float) -> None:
        self.cfg = cfg
        self.key = secret("GEMINI_API_KEY") or secret("GOOGLE_API_KEY")
        self._client = None
        self._timeout_ms = int(timeout_sec * 1000)
        self._no_thinking_cfg: set[str] = set()
        # Google Search grounding has its own free quota (often 0): when it fails, answer without it for a while.
        self._search_blocked_until = 0.0

    def available(self) -> bool:
        return bool(self.key and self.cfg.models)

    def _search_allowed(self) -> bool:
        return time.monotonic() >= self._search_blocked_until

    def can_search(self) -> bool:
        return self.cfg.web_search and self._search_allowed()

    def _get_client(self):
        if self._client is None:
            from google import genai
            from google.genai import types

            self._client = genai.Client(api_key=self.key, http_options=types.HttpOptions(timeout=self._timeout_ms))
        return self._client

    def _config(self, system: str, web: bool, max_tokens: int, model: str):
        from google.genai import types

        kwargs: dict[str, Any] = {
            "system_instruction": system or None,
            "max_output_tokens": max_tokens,
            "temperature": 0.5,
            # We never pass Python callables as tools; AFC only adds a warning.
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if web and self.cfg.web_search and self._search_allowed():
            kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        if model not in self._no_thinking_cfg:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
        return types.GenerateContentConfig(**kwargs)

    async def stream(self, messages: list[Msg], *, web: bool, max_tokens: int | None = None,
                     models: list[str] | None = None) -> AsyncIterator[str]:
        from google.genai import errors, types

        client = self._get_client()
        system = "\n".join(m.content for m in messages if m.role == "system")
        contents = []
        for m in messages:
            if m.role not in ("user", "assistant") or not (m.content or m.images):
                continue
            parts = [types.Part.from_bytes(data=img, mime_type="image/png") for img in m.images]
            if m.content:
                parts.append(types.Part(text=m.content))
            contents.append(types.Content(role="model" if m.role == "assistant" else "user", parts=parts))
        last_exc: Exception | None = None
        for model in models or self.cfg.models:
            for _attempt in range(3):
                searching = web and self.cfg.web_search and self._search_allowed()
                first = True
                try:
                    cfg = self._config(system, web, max_tokens or self.cfg.max_output_tokens, model)
                    stream = await client.aio.models.generate_content_stream(model=model, contents=contents, config=cfg)
                    async for chunk in stream:
                        meta = getattr(chunk, "usage_metadata", None)
                        if meta is not None and meta.prompt_token_count:
                            self.last_usage = (meta.prompt_token_count or 0,
                                               (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0))
                        text = chunk.text or ""
                        if text:
                            if first:
                                log.info("gemini/%s отвечает", model)
                                first = False
                            yield text
                    return
                except errors.APIError as exc:
                    last_exc = exc
                    code = getattr(exc, "code", 0)
                    if code == 400 and model not in self._no_thinking_cfg and "think" in str(exc).lower():
                        self._no_thinking_cfg.add(model)  # older model without thinking levels
                        continue
                    if _is_auth_problem(code, str(exc)):
                        raise AuthError("gemini: неверный GEMINI_API_KEY или нет доступа из региона (VPN?)") from exc
                    if code == 429 and searching:
                        self._search_blocked_until = time.monotonic() + SEARCH_BLOCK_SEC
                        log.warning("gemini: поиск Google недоступен на этом ключе, отвечаю без поиска")
                        continue
                    log.warning("gemini/%s: %s %s", model, code, str(exc)[:160])
                    break  # 429 / 404 / 5xx: try the next model
                except (asyncio.TimeoutError, TimeoutError, OSError) as exc:
                    # The full Flash often hangs on the free tier: the next (lighter) model answers in a second.
                    if not first:
                        raise ProviderError(f"gemini/{model}: оборвалось посреди ответа") from exc
                    last_exc = exc
                    log.warning("gemini/%s не ответил вовремя, пробую следующую модель", model)
                    break
        if last_exc is not None and getattr(last_exc, "code", 0) == 429:
            text = str(last_exc)
            raise QuotaError(f"gemini: {last_exc}", parse_wait(text), _is_daily(text))
        raise ProviderError(f"gemini: {last_exc}")


# --------------------------------------------------------------------------- Groq
class GroqProvider(Provider):
    name = "groq"
    BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self, cfg: CloudCfg, timeout_sec: float) -> None:
        self.cfg = cfg
        self.key = secret("GROQ_API_KEY")
        self._client = None
        self._timeout = timeout_sec

    def available(self) -> bool:
        return bool(self.key and self.cfg.models)

    def can_search(self) -> bool:
        return self.cfg.web_search and any(m.startswith("openai/gpt-oss") for m in self.cfg.models)

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self.key, base_url=self.BASE_URL, timeout=self._timeout, max_retries=0)
        return self._client

    async def stream(self, messages: list[Msg], *, web: bool, max_tokens: int | None = None,
                     models: list[str] | None = None) -> AsyncIterator[str]:
        import openai

        client = self._get_client()
        payload = [{"role": m.role, "content": m.content} for m in messages if m.role in ("system", "user", "assistant")]
        last_exc: Exception | None = None
        quota = False
        waits: list[float] = []
        daily = True
        for model in self.cfg.models:   # the override is for Gemini models; Groq keeps its own
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": payload,
                "max_completion_tokens": max_tokens or self.cfg.max_output_tokens,
                "temperature": 0.5,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if model.startswith("openai/gpt-oss"):
                kwargs["reasoning_effort"] = "low"
                kwargs["extra_body"] = {"include_reasoning": False}
                if web and self.cfg.web_search:
                    kwargs["tools"] = [{"type": "browser_search"}]
            try:
                stream = await client.chat.completions.create(**kwargs)
                first = True
                async for chunk in stream:
                    if getattr(chunk, "usage", None):
                        self.last_usage = (chunk.usage.prompt_tokens or 0, chunk.usage.completion_tokens or 0)
                    if not chunk.choices:
                        continue
                    text = chunk.choices[0].delta.content or ""
                    if text:
                        if first:
                            log.info("groq/%s отвечает", model)
                            first = False
                        yield text
                return
            except openai.RateLimitError as exc:
                last_exc, quota = exc, True
                header = exc.response.headers.get("retry-after") if exc.response is not None else None
                wait = float(header) if header and header.replace(".", "", 1).isdigit() else parse_wait(str(exc))
                waits.append(wait if wait is not None else 60.0)
                daily = daily and _is_daily(str(exc))
                log.warning("groq/%s: лимит%s, ждать %s с", model, " дневной" if _is_daily(str(exc)) else "",
                            round(waits[-1]))
            except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
                raise AuthError("groq: неверный GROQ_API_KEY или нет доступа из региона (VPN?)") from exc
            except (openai.APIStatusError, openai.APIConnectionError, openai.APITimeoutError) as exc:
                last_exc = exc
                log.warning("groq/%s: %s", model, str(exc)[:160])
        if quota:
            # Every model hit its limit: the shortest wait decides when Groq is worth trying again.
            raise QuotaError(f"groq: {last_exc}", min(waits) if waits else None, daily)
        raise ProviderError(f"groq: {last_exc}")


# --------------------------------------------------------------------------- Ollama
@dataclass
class LocalTurn:
    """Result of one local model call: streamed text already yielded + requested tool calls."""
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class OllamaProvider(Provider):
    name = "local"

    def __init__(self, cfg: LocalLlmCfg) -> None:
        from ollama import AsyncClient

        self.cfg = cfg
        self.client = AsyncClient(host=cfg.host)
        self.healthy = False  # set by warmup(); the router prefers the local model when it runs

    def available(self) -> bool:
        return True

    def _options(self, max_tokens: int | None) -> dict[str, Any]:
        opts: dict[str, Any] = {"num_ctx": self.cfg.num_ctx, "temperature": self.cfg.temperature}
        if max_tokens:
            opts["num_predict"] = max_tokens
        return opts

    @staticmethod
    def _to_ollama(messages: list[Msg]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            d: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.images:
                d["images"] = m.images
            if m.tool_calls:
                d["tool_calls"] = m.tool_calls
            if m.role == "tool" and m.tool_name:
                d["tool_name"] = m.tool_name
            out.append(d)
        return out

    async def warmup(self, model: str = "") -> bool:
        model = model or self.cfg.model
        try:
            await self.client.chat(model=model, messages=[{"role": "user", "content": "привет"}],
                                   think=False, keep_alive=self.cfg.keep_alive, options={"num_predict": 1})
            if model == self.cfg.model:
                self.healthy = True
            log.info("Локальная модель %s загружена", model)
            return True
        except Exception as exc:
            if model == self.cfg.model:
                self.healthy = False
            log.warning("Модель %s недоступна (%s). Запустите Ollama или скачайте модель: ollama pull %s", model, exc, model)
            return False

    async def alive(self) -> bool:
        """Is the Ollama server answering (cheap, no model is loaded)."""
        try:
            await asyncio.wait_for(self.client.ps(), timeout=3)
            return True
        except Exception:
            return False

    async def unload(self, model: str) -> None:
        """Frees the model's video memory now instead of after keep_alive (30 min)."""
        try:
            await self.client.generate(model=model, prompt="", keep_alive=0)
            log.info("Модель %s выгружена из памяти", model)
        except Exception as exc:
            log.debug("Не выгрузил %s: %s", model, exc)

    async def stream(self, messages: list[Msg], *, web: bool = False, max_tokens: int | None = None,
                     models: list[str] | None = None) -> AsyncIterator[str]:
        vision = any(m.images for m in messages)
        model = self.cfg.vision_model if vision else self.cfg.model
        think: bool | None = False
        stream = None
        for _ in range(2):
            try:
                stream = await self.client.chat(model=model, messages=self._to_ollama(messages), stream=True, think=think,
                                                keep_alive=self.cfg.keep_alive, options=self._options(max_tokens))
                break
            except Exception as exc:
                if think is not None and "think" in str(exc).lower():
                    think = None  # model without a thinking switch
                    continue
                raise ProviderError(f"ollama/{model}: {exc}") from exc
        async for part in stream:
            if part.done:
                self.last_usage = (part.prompt_eval_count or 0, part.eval_count or 0)
            text = part.message.content or ""
            if text:
                yield text

    async def stream_with_tools(self, messages: list[Msg], tools: list[dict[str, Any]], turn: LocalTurn,
                                max_tokens: int | None = None) -> AsyncIterator[str]:
        """Streams text; tool calls are collected into `turn.tool_calls`."""
        try:
            stream = await self.client.chat(model=self.cfg.model, messages=self._to_ollama(messages), tools=tools,
                                            stream=True, think=False, keep_alive=self.cfg.keep_alive,
                                            options=self._options(max_tokens))
        except Exception as exc:
            raise ProviderError(f"ollama: {exc}") from exc
        async for part in stream:
            for call in part.message.tool_calls or []:
                turn.tool_calls.append({"function": {"name": call.function.name, "arguments": dict(call.function.arguments or {})}})
            text = part.message.content or ""
            if text:
                turn.text += text
                yield text

    async def complete(self, messages: list[Msg], *, fmt: dict | str | None = None, max_tokens: int | None = None,
                       model: str = "") -> str:
        try:
            resp = await self.client.chat(model=model or self.cfg.model, messages=self._to_ollama(messages), think=False,
                                          format=fmt, keep_alive=self.cfg.keep_alive, options=self._options(max_tokens))
        except Exception as exc:
            raise ProviderError(f"ollama: {exc}") from exc
        return resp.message.content or ""
