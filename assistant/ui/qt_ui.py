"""Qt UI: tray icon + explanation window. All core->UI calls go through queued signals."""
from __future__ import annotations

import concurrent.futures
import copy
import html
import logging
import os
import subprocess
import webbrowser
from typing import TYPE_CHECKING

from PySide6.QtCore import (QBuffer, QByteArray, QEasingCurve, QIODevice, QObject, QPoint, QPropertyAnimation, QRect,
                            QRectF, Qt, QTimer, Signal)
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QGraphicsOpacityEffect, QHBoxLayout, QInputDialog, QLabel, QMenu,
                               QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)

from assistant.core import STATE_LABELS, Deck, State

if TYPE_CHECKING:
    from assistant.assistant import Assistant

log = logging.getLogger("ui")

STATE_COLORS = {
    State.IDLE: "#5B6B7F",
    State.LISTENING: "#2F9E6B",
    State.THINKING: "#D9A62E",
    State.SPEAKING: "#3F7FE0",
    State.MUTED: "#B04A4A",
    State.ERROR: "#B04A4A",
}


def state_icon(state: State | None, size: int = 64) -> QIcon:
    color = QColor(STATE_COLORS.get(state, "#5B6B7F") if state else "#888888")
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(color)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(2, 2, size - 4, size - 4)
    p.setPen(QColor("white"))
    f = QFont("Segoe UI", int(size * 0.45))
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "J")
    if state is State.MUTED:
        p.setPen(QColor("white"))
        pen = p.pen()
        pen.setWidth(max(3, size // 12))
        p.setPen(pen)
        p.drawLine(int(size * 0.2), int(size * 0.8), int(size * 0.8), int(size * 0.2))
    p.end()
    return QIcon(pm)


class DeckWindow(QWidget):
    """Frameless glass card window in the bottom-right corner."""

    WIDTH = 440

    def __init__(self, linger_sec: float) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(self.WIDTH)
        self.deck: Deck | None = None
        self.index = 0
        self._drag: QPoint | None = None
        self._user_pos: QPoint | None = None  # where the user dragged the window (kept until restart)
        self._close_timer = QTimer(self, singleShot=True, interval=int(linger_sec * 1000))
        self._close_timer.timeout.connect(self.fade_out)

        self.setStyleSheet("""
            QLabel { color: #E8ECF3; background: transparent; }
            QLabel#title { color: #8FA3BF; font: 600 10pt 'Segoe UI'; letter-spacing: 1px; }
            QLabel#heading { font: 600 15pt 'Segoe UI'; color: #FFFFFF; }
            QLabel#body { font: 11.5pt 'Segoe UI'; color: #D6DCE6; }
            QLabel#counter { color: #7D8BA0; font: 9pt 'Segoe UI'; }
            QPushButton { background: rgba(255,255,255,0.08); color: #E8ECF3; border: none; border-radius: 8px;
                          padding: 4px 10px; font: 11pt 'Segoe UI'; }
            QPushButton:hover { background: rgba(255,255,255,0.18); }
        """)
        self.title = QLabel(objectName="title")
        self.heading = QLabel(objectName="heading", wordWrap=True)
        self.body = QLabel(objectName="body", wordWrap=True, textFormat=Qt.TextFormat.RichText)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.counter = QLabel(objectName="counter")
        prev_btn, next_btn, close_btn = QPushButton("‹"), QPushButton("›"), QPushButton("✕")
        prev_btn.clicked.connect(lambda: self.show_card(self.index - 1, manual=True))
        next_btn.clicked.connect(lambda: self.show_card(self.index + 1, manual=True))
        close_btn.clicked.connect(self.fade_out)

        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(close_btn)
        bottom = QHBoxLayout()
        bottom.addWidget(self.counter, 1)
        bottom.addWidget(prev_btn)
        bottom.addWidget(next_btn)
        self.content = QWidget()
        inner = QVBoxLayout(self.content)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(10)
        inner.addWidget(self.heading)
        inner.addWidget(self.body)
        self.fx = QGraphicsOpacityEffect(self.content)
        self.content.setGraphicsEffect(self.fx)
        self.anim = QPropertyAnimation(self.fx, b"opacity", self, duration=260, easingCurve=QEasingCurve.Type.OutCubic)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(12)
        root.addLayout(top)
        root.addWidget(self.content)
        root.addLayout(bottom)

        self.win_anim = QPropertyAnimation(self, b"windowOpacity", self, duration=220)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 18, 18)
        p.fillPath(path, QColor(20, 24, 33, 236))
        p.setPen(QColor(255, 255, 255, 30))
        p.drawPath(path)
        accent = QPainterPath()
        accent.addRoundedRect(QRectF(22, 0, 56, 3), 1.5, 1.5)
        p.fillPath(accent, QColor("#3F7FE0"))

    # dragging
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)
            self._user_pos = self.pos()

    def mouseReleaseEvent(self, _e) -> None:
        self._drag = None

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.fade_out()
        elif e.key() in (Qt.Key.Key_Right, Qt.Key.Key_Space):
            self.show_card(self.index + 1, manual=True)
        elif e.key() == Qt.Key.Key_Left:
            self.show_card(self.index - 1, manual=True)

    def _place(self) -> None:
        """Bottom-right by default; after the user drags it, keep their spot (only clamp to the screen)."""
        self.adjustSize()
        if self._user_pos is None:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)
            return
        screen = (QGuiApplication.screenAt(self._user_pos) or QApplication.primaryScreen()).availableGeometry()
        x = min(max(self._user_pos.x(), screen.left()), screen.right() - self.width())
        y = min(max(self._user_pos.y(), screen.top()), screen.bottom() - self.height())
        self.move(x, y)

    def set_deck(self, deck: Deck) -> None:
        self.deck = copy.deepcopy(deck)
        self.title.setText(html.escape(deck.title.upper()))
        self._close_timer.stop()
        if deck.cards:
            self.show_card(0)
        else:
            self.heading.setText("Готовлю…")
            self.body.setText("")
            self.counter.setText("")
        self.setWindowOpacity(1.0)
        self.show()
        self._place()
        if deck.done:
            self._close_timer.start()

    def update_deck(self, deck: Deck) -> None:
        if self.deck is None:
            return self.set_deck(deck)
        self.deck = copy.deepcopy(deck)
        self.title.setText(html.escape(deck.title.upper()))
        self._update_counter()
        if deck.done:
            self._close_timer.start()

    def _update_counter(self) -> None:
        if self.deck and len(self.deck.cards) > 1:
            self.counter.setText(f"{self.index + 1} / {len(self.deck.cards)}{'' if self.deck.done else ' …'}")
        else:
            self.counter.setText("")

    def show_card(self, index: int, manual: bool = False) -> None:
        if not self.deck or not self.deck.cards:
            return
        index = max(0, min(index, len(self.deck.cards) - 1))
        card = self.deck.cards[index]
        self.index = index
        self.heading.setText(html.escape(card.heading))
        self.heading.setVisible(bool(card.heading))
        self.body.setText(_render_screen(card.screen))
        self._update_counter()
        self.anim.stop()
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.start()
        if not self.isVisible():
            self.show()
        self._place()
        if manual or (self.deck.done and index == len(self.deck.cards) - 1):
            self._close_timer.start()

    def fade_out(self) -> None:
        self._close_timer.stop()
        self.win_anim.stop()
        self.win_anim.setStartValue(self.windowOpacity())
        self.win_anim.setEndValue(0.0)
        try:
            self.win_anim.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.win_anim.finished.connect(self.hide)
        self.win_anim.start()


class RegionSelector(QWidget):
    """Freeze-frame of the screen under the cursor; drag a rectangle, Esc cancels. Nothing touches the disk."""

    def __init__(self, fut: "concurrent.futures.Future[bytes | None]") -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.fut = fut
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self.shot = screen.grabWindow(0)  # physical pixels, devicePixelRatio set
        self.dpr = self.shot.devicePixelRatio()
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.origin: QPoint | None = None
        self.sel = QRect()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.shot)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if not self.sel.isNull():
            src = QRect(int(self.sel.x() * self.dpr), int(self.sel.y() * self.dpr),
                        int(self.sel.width() * self.dpr), int(self.sel.height() * self.dpr))
            p.drawPixmap(self.sel, self.shot, src)
            p.setPen(QPen(QColor("#3F7FE0"), 2))
            p.drawRect(self.sel)
        else:
            p.setPen(QColor("white"))
            p.setFont(QFont("Segoe UI", 14))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Выделите область, с которой помочь  ·  Esc — отмена")

    def mousePressEvent(self, e) -> None:
        self.origin = e.position().toPoint()
        self.sel = QRect(self.origin, self.origin)
        self.update()

    def mouseMoveEvent(self, e) -> None:
        if self.origin is not None:
            self.sel = QRect(self.origin, e.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, _e) -> None:
        if self.sel.width() < 8 or self.sel.height() < 8:
            return self._finish(None)
        src = QRect(int(self.sel.x() * self.dpr), int(self.sel.y() * self.dpr),
                    int(self.sel.width() * self.dpr), int(self.sel.height() * self.dpr))
        crop = self.shot.copy(src)
        if max(crop.width(), crop.height()) > 1600:
            crop = crop.scaled(1600, 1600, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        crop.save(buf, "PNG")
        self._finish(bytes(data))

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self._finish(None)

    def _finish(self, png: bytes | None) -> None:
        if not self.fut.done():
            self.fut.set_result(png)
        self.close()
        self.deleteLater()


def _render_screen(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bullets = [ln[1:].strip() for ln in lines if ln.startswith(("-", "•"))]
    if lines and len(bullets) == len(lines):
        items = "".join(f"<tr><td style='color:#3F7FE0;padding-right:8px'>●</td><td style='padding-bottom:6px'>{html.escape(b)}</td></tr>" for b in bullets)
        return f"<table cellspacing='0'>{items}</table>"
    return "<br>".join(html.escape(ln) for ln in lines)


class QtUi(QObject):
    """UiPort implementation. Methods may be called from any thread."""

    _state = Signal(object, str)
    _notify = Signal(str, str)
    _show_deck = Signal(object)
    _update_deck = Signal(object)
    _show_card = Signal(int)
    _close_deck = Signal()
    _open_url = Signal(str)
    _select_region = Signal(object)
    quit_requested = Signal()

    def __init__(self, linger_sec: float) -> None:
        super().__init__()
        self.window = DeckWindow(linger_sec)
        self.tray = QSystemTrayIcon(state_icon(None))
        self.tray.setToolTip("Джарвис — загрузка…")
        self.assistant: Assistant | None = None
        self._status_action = QAction("Загрузка…")
        self._status_action.setEnabled(False)
        self._mute_action = QAction("Выключить микрофон")
        self._state.connect(self._on_state)
        self._notify.connect(lambda t, m: self.tray.showMessage(t, m, state_icon(State.SPEAKING), 8000))
        self._show_deck.connect(self.window.set_deck)
        self._update_deck.connect(self.window.update_deck)
        self._show_card.connect(self.window.show_card)
        self._close_deck.connect(self.window.fade_out)
        self._open_url.connect(webbrowser.open)
        self._select_region.connect(self._show_selector)
        self._selector: RegionSelector | None = None

    # UiPort
    def set_state(self, state: State, detail: str = "") -> None:
        self._state.emit(state, detail)

    def notify(self, title: str, text: str) -> None:
        self._notify.emit(title, text)

    def show_deck(self, deck: Deck) -> None:
        self._show_deck.emit(copy.deepcopy(deck))

    def update_deck(self, deck: Deck) -> None:
        self._update_deck.emit(copy.deepcopy(deck))

    def show_card(self, index: int) -> None:
        self._show_card.emit(index)

    def close_deck(self) -> None:
        self._close_deck.emit()

    def open_url(self, url: str) -> None:
        self._open_url.emit(url)

    def select_region(self) -> "concurrent.futures.Future[bytes | None]":
        fut: concurrent.futures.Future[bytes | None] = concurrent.futures.Future()
        self._select_region.emit(fut)
        return fut

    def _show_selector(self, fut) -> None:
        self._selector = RegionSelector(fut)
        self._selector.show()
        self._selector.activateWindow()
        self._selector.raise_()

    # tray
    def attach(self, assistant: "Assistant", on_quit) -> None:
        self.assistant = assistant
        a = assistant
        menu = QMenu()
        menu.addAction(self._status_action)
        menu.addSeparator()
        listen = menu.addAction("Слушать сейчас  (Ctrl+Alt+J)")
        listen.triggered.connect(lambda: a._threadsafe(a.listen_now))
        self._mute_action.triggered.connect(lambda: a._threadsafe(a.toggle_mute))
        menu.addAction(self._mute_action)
        ask = menu.addAction("Спросить текстом…")
        ask.triggered.connect(self._ask_text)
        menu.addSeparator()
        screen = menu.addAction("Помочь с экраном  (Ctrl+Alt+S)")
        screen.triggered.connect(lambda: a.submit(a.handle_text("что на экране")))
        menu.addSeparator()
        from assistant.skills.scenarios import scenarios_file

        scen = menu.addAction("Мои сценарии…")
        scen.triggered.connect(lambda: subprocess.Popen(["notepad.exe", str(scenarios_file())]))
        notes = menu.addAction("Папка заметок")
        notes.triggered.connect(lambda: os.startfile(a.cfg.resolve(a.cfg.notes.dir)))  # type: ignore[attr-defined]
        settings = menu.addAction("Настройки…")
        settings.triggered.connect(lambda: webbrowser.open(a.voicelab_url))
        menu.addSeparator()
        quit_action = menu.addAction("Выход")
        quit_action.triggered.connect(on_quit)
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_click)
        self.tray.show()

    def _on_click(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger and self.assistant:
            self.assistant._threadsafe(self.assistant.listen_now)

    def _ask_text(self) -> None:
        text, ok = QInputDialog.getText(None, "Джарвис", "Вопрос или команда:")
        if ok and text.strip() and self.assistant:
            self.assistant.submit(self.assistant.handle_text(text.strip()))

    def _on_state(self, state: State, detail: str) -> None:
        label = STATE_LABELS.get(state, state.value)
        self.tray.setIcon(state_icon(state))
        tip = f"Джарвис — {label}" + (f"\n{detail[:80]}" if detail else "")
        self.tray.setToolTip(tip)
        self._status_action.setText(label)
        self._mute_action.setText("Включить микрофон" if state is State.MUTED else "Выключить микрофон")
