"""Provider chains with fallback and quota cooldowns."""
from __future__ import annotations

import logging
import time
from typing import AsyncIterator

from assistant.config import LlmCfg
from assistant.llm.usage import Usage
from assistant.llm.providers import (AuthError, GeminiProvider, GroqProvider, Msg, OllamaProvider, Provider, ProviderError,
                                     QuotaError)

log = logging.getLogger("llm")

FAIL_TEXT = "Не получилось получить ответ: ни облако, ни локальная модель сейчас не отвечают."
AUTH_COOLDOWN_SEC = 3600
OFFLINE_NOTE = ("Доступа к интернету сейчас нет. Не выдумывай свежие данные: курсы, цены, новости, счёт матчей, "
                "погоду и события после твоего обучения. Если вопрос требует таких данных — коротко скажи, "
                "что сейчас не можешь это проверить. Общеизвестные факты и определения объясняй как обычно.")


class LlmHub:
    def __init__(self, cfg: LlmCfg) -> None:
        self.cfg = cfg
        self.local = OllamaProvider(cfg.local)
        self.providers: dict[str, Provider] = {
            "gemini": GeminiProvider(cfg.gemini, cfg.request_timeout_sec),
            "groq": GroqProvider(cfg.groq, cfg.request_timeout_sec),
            "local": self.local,
        }
        self._cooldown_until: dict[str, float] = {}
        self.last_provider = ""
        self.usage = Usage()

    def status(self) -> dict[str, str]:
        now = time.monotonic()
        out = {}
        for name, p in self.providers.items():
            if not p.available():
                out[name] = "нет ключа"
            elif self._over_budget(name):
                out[name] = "дневной лимит"
            elif self._cooldown_until.get(name, 0) > now:
                out[name] = f"пауза {int(self._cooldown_until[name] - now)} с"
            else:
                out[name] = "готов"
        return out

    def _over_budget(self, name: str) -> bool:
        limit = self.cfg.daily_limits.get(name)
        return bool(limit) and self.usage.requests_today(name) >= limit

    def _usable(self, chain: list[str]) -> list[Provider]:
        now = time.monotonic()
        return [self.providers[n] for n in chain
                if n in self.providers and self.providers[n].available() and self._cooldown_until.get(n, 0) <= now
                and not self._over_budget(n)]

    async def stream(self, chain: list[str], messages: list[Msg], *, web: bool,
                     max_tokens: int | None = None) -> AsyncIterator[str]:
        """Yields deltas from the first provider that starts answering."""
        for provider in self._usable(chain):
            started = False
            provider.last_usage = (0, 0)
            msgs = messages
            if web and provider is self.local:
                msgs = [Msg("system", OFFLINE_NOTE), *messages]
            try:
                async for delta in provider.stream(msgs, web=web, max_tokens=max_tokens):
                    if not started:
                        started = True
                        self.last_provider = provider.name
                    yield delta
                if started:
                    self.usage.record(provider.name, *provider.last_usage)
                    log.debug("%s: %s токенов", provider.name, provider.last_usage)
                    return
                log.warning("%s вернул пустой ответ", provider.name)
            except AuthError as exc:
                self._cooldown_until[provider.name] = time.monotonic() + AUTH_COOLDOWN_SEC
                log.error("%s отключён на час: %s", provider.name, exc)
            except QuotaError as exc:
                self._cooldown_until[provider.name] = time.monotonic() + self.cfg.cooldown_sec
                log.warning("%s: лимит исчерпан, пауза %d с (%s)", provider.name, self.cfg.cooldown_sec, str(exc)[:120])
            except ProviderError as exc:
                log.warning("%s недоступен: %s", provider.name, str(exc)[:200])
            except Exception:
                log.exception("%s: непредвиденная ошибка", provider.name)
            if started:
                return  # failed mid-answer: keep what was said, do not start over
        yield FAIL_TEXT

    async def complete(self, chain: list[str], messages: list[Msg], *, web: bool, max_tokens: int | None = None) -> str:
        parts = [d async for d in self.stream(chain, messages, web=web, max_tokens=max_tokens)]
        return "".join(parts).strip()
