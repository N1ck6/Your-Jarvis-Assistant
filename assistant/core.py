"""Shared types: assistant state, UI port, card payloads."""
from __future__ import annotations

import concurrent.futures
import enum
import logging
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger("core")


class State(str, enum.Enum):
    IDLE = "idle"            # waiting for the wake word
    LISTENING = "listening"  # recording a command
    THINKING = "thinking"
    SPEAKING = "speaking"
    MUTED = "muted"          # microphone released
    ERROR = "error"


STATE_LABELS = {
    State.IDLE: "Жду «Джарвис»",
    State.LISTENING: "Слушаю",
    State.THINKING: "Думаю",
    State.SPEAKING: "Говорю",
    State.MUTED: "Микрофон выключен",
    State.ERROR: "Ошибка",
}


@dataclass
class Card:
    heading: str
    screen: str   # text shown in the window (short lines, "- " bullets allowed)
    speech: str   # what the voice says while the user reads


@dataclass
class Deck:
    title: str
    cards: list[Card] = field(default_factory=list)
    done: bool = False  # False while cards are still streaming in


class UiPort(Protocol):
    """What the core may ask of the UI. Implementations must be thread-safe."""

    def set_state(self, state: State, detail: str = "") -> None: ...
    def notify(self, title: str, text: str) -> None: ...
    def show_deck(self, deck: Deck) -> None: ...
    def update_deck(self, deck: Deck) -> None: ...
    def show_card(self, index: int) -> None: ...
    def close_deck(self) -> None: ...
    def open_url(self, url: str) -> None: ...
    def open_settings(self, url: str) -> None:
        """The settings page in a window of its own (not a browser tab)."""
        ...
    def show_visual(self, visual) -> None:
        """A picture next to the cards (assistant.visuals.Visual)."""
        ...
    def close_visual(self) -> None: ...
    def chat_add(self, role: str, text: str) -> None:
        """A line for the chat window history: role "user" or "assistant"."""
        ...
    def select_region(self) -> "concurrent.futures.Future[bytes | None]":
        """Freeze-frame of the screen, the user drags a rectangle; resolves to PNG bytes or None."""
        ...


class ConsoleUi:
    """Headless UI used with --no-ui and in tests."""

    def set_state(self, state: State, detail: str = "") -> None:
        from assistant.log import private

        log.info("[%s] %s", STATE_LABELS.get(state, state.value), private(detail))

    def notify(self, title: str, text: str) -> None:
        from assistant.log import private

        log.info("УВЕДОМЛЕНИЕ %s: %s", title, private(text))

    def show_deck(self, deck: Deck) -> None:
        from assistant.log import private

        log.info("ОКНО «%s»", private(deck.title))

    def update_deck(self, deck: Deck) -> None:
        pass

    def show_card(self, index: int) -> None:
        log.info("  карточка %d", index + 1)

    def close_deck(self) -> None:
        pass

    def open_url(self, url: str) -> None:
        import webbrowser

        webbrowser.open(url)

    def open_settings(self, url: str) -> None:
        self.open_url(url)

    def show_visual(self, visual) -> None:
        from assistant.log import private

        log.info("КАРТИНКА «%s»", private(visual.title))

    def close_visual(self) -> None:
        pass

    def chat_add(self, role: str, text: str) -> None:
        pass

    def select_region(self) -> "concurrent.futures.Future[bytes | None]":
        fut: concurrent.futures.Future[bytes | None] = concurrent.futures.Future()
        fut.set_result(None)  # no screen UI in console mode
        return fut
