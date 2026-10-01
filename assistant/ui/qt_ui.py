"""Qt UI: tray icon + explanation window. All core->UI calls go through queued signals."""
from __future__ import annotations

import concurrent.futures
import copy
import html
import logging
import os
import re
import subprocess
import webbrowser
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import (QBuffer, QByteArray, QEasingCurve, QIODevice, QObject, QPoint, QPropertyAnimation, QRect,
                            QRectF, Qt, QTimer, Signal)
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
                               QScrollArea, QSystemTrayIcon, QTextBrowser, QVBoxLayout, QWidget)

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
    """Frameless glass card window in the bottom-right corner (or wherever the user dragged it)."""

    WIDTH = 540
    MARGIN = 22

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
        # Set by QtUi: the user paged by hand / closed the window (the core speaks or stops).
        self.on_user_nav: Callable[[int], None] = lambda _i: None
        self.on_user_close: Callable[[], None] = lambda: None

        self.setStyleSheet("""
            QLabel { color: #E8ECF3; background: transparent; }
            QLabel#title { color: #8FA3BF; font: 600 10pt 'Segoe UI'; letter-spacing: 1px; }
            QLabel#heading { font: 600 15pt 'Segoe UI'; color: #FFFFFF; }
            QLabel#body { font: 11pt 'Segoe UI'; color: #D6DCE6; }
            QLabel#counter { color: #7D8BA0; font: 9pt 'Segoe UI'; }
            QPushButton { background: rgba(255,255,255,0.08); color: #E8ECF3; border: none; border-radius: 8px;
                          padding: 4px 10px; font: 11pt 'Segoe UI'; }
            QPushButton:hover { background: rgba(255,255,255,0.18); }
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,0.25); border-radius: 3px; min-height: 24px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        self.title = QLabel(objectName="title")
        self.heading = QLabel(objectName="heading", wordWrap=True)
        self.body = QLabel(objectName="body", wordWrap=True, textFormat=Qt.TextFormat.RichText)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.counter = QLabel(objectName="counter")
        prev_btn, next_btn, close_btn = QPushButton("‹"), QPushButton("›"), QPushButton("✕")
        self.copy_btn = QPushButton("Копировать")
        self.copy_btn.setToolTip("Скопировать текст карточки")
        prev_btn.clicked.connect(lambda: self._user_nav(self.index - 1))
        next_btn.clicked.connect(lambda: self._user_nav(self.index + 1))
        close_btn.clicked.connect(self._user_close)
        self.copy_btn.clicked.connect(self._copy)

        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(close_btn)
        bottom = QHBoxLayout()
        bottom.addWidget(self.counter, 1)
        bottom.addWidget(self.copy_btn)
        bottom.addWidget(prev_btn)
        bottom.addWidget(next_btn)
        self.content = QWidget()
        inner = QVBoxLayout(self.content)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(10)
        inner.addWidget(self.heading)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.body)
        self.scroll.viewport().setStyleSheet("background: transparent;")
        inner.addWidget(self.scroll)
        self.fx = QGraphicsOpacityEffect(self.content)
        self.content.setGraphicsEffect(self.fx)
        self.anim = QPropertyAnimation(self.fx, b"opacity", self, duration=260, easingCurve=QEasingCurve.Type.OutCubic)

        root = QVBoxLayout(self)
        root.setContentsMargins(self.MARGIN, 16, self.MARGIN, 16)
        root.setSpacing(12)
        root.addLayout(top)
        root.addWidget(self.content)
        root.addLayout(bottom)

        self.win_anim = QPropertyAnimation(self, b"windowOpacity", self, duration=220)
        self.win_anim.finished.connect(self._faded)

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
            self._user_close()
        elif e.key() in (Qt.Key.Key_Right, Qt.Key.Key_Space):
            self._user_nav(self.index + 1)
        elif e.key() == Qt.Key.Key_Left:
            self._user_nav(self.index - 1)

    def _user_nav(self, index: int) -> None:
        if not self.deck or not self.deck.cards:
            return
        index = max(0, min(index, len(self.deck.cards) - 1))
        if index == self.index:
            return
        self.show_card(index, manual=True)
        self.on_user_nav(index)

    def _user_close(self) -> None:
        self.fade_out()
        self.on_user_close()

    def _copy(self) -> None:
        if self.deck and self.deck.cards:
            card = self.deck.cards[self.index]
            QApplication.clipboard().setText((card.heading + "\n" if card.heading else "") + card.screen)
            self.copy_btn.setText("Скопировано")
            QTimer.singleShot(1500, lambda: self.copy_btn.setText("Копировать"))

    def _fit(self) -> None:
        """The text area grows with the card up to ~60% of the screen, then scrolls."""
        screen = (QGuiApplication.screenAt(self.pos()) or QApplication.primaryScreen()).availableGeometry()
        width = self.WIDTH - 2 * self.MARGIN - 8
        need = self.body.heightForWidth(width) + 6
        self.scroll.setFixedHeight(max(40, min(need, int(screen.height() * 0.6))))

    def _place(self) -> None:
        """Bottom-right by default; after the user drags it, keep their spot (only clamp to the screen)."""
        self._fit()
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
        self.win_anim.stop()
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
        self.body.setText(render_screen(card.screen))
        self.scroll.verticalScrollBar().setValue(0)
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
        self.win_anim.start()

    def _faded(self) -> None:
        if self.windowOpacity() < 0.05:  # a new deck may have stopped the fade halfway
            self.hide()


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


_TERM = re.compile(r"^(.{2,48}?)\s+[—–-]\s+(.+)$")
_STEP = re.compile(r"^(\d{1,2})[.)]\s+(.+)$")


def _term(text: str) -> str:
    """'Термин — пояснение' -> bold term."""
    m = _TERM.match(text)
    if m and len(m.group(1).split()) <= 5:
        return f"<b style='color:#FFFFFF'>{html.escape(m.group(1))}</b> — {html.escape(m.group(2))}"
    return html.escape(text)


def render_screen(text: str) -> str:
    """Card text -> Qt rich text: '- ' bullets, '1. ' steps, '> ' code/formula lines, plain paragraphs."""
    rows: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("-", "•", "*")) and not line.startswith("**"):
            rows.append(f"<tr><td width='18' style='color:#3F7FE0'>●</td>"
                        f"<td style='padding-bottom:7px'>{_term(line[1:].strip())}</td></tr>")
        elif m := _STEP.match(line):
            rows.append(f"<tr><td width='18' style='color:#3F7FE0;font-weight:600'>{m.group(1)}</td>"
                        f"<td style='padding-bottom:7px'>{_term(m.group(2))}</td></tr>")
        elif line.startswith((">", "`")):
            code = line.lstrip(">` ").rstrip("`")
            rows.append(f"<tr><td width='18'></td><td style='padding-bottom:7px'><table cellpadding='6' width='100%' "
                        f"style='background-color:#0F131B'><tr><td style=\"font-family:'Cascadia Mono',Consolas,monospace;"
                        f"font-size:10pt;color:#9FD3A6\">{html.escape(code)}</td></tr></table></td></tr>")
        else:
            rows.append(f"<tr><td colspan='2' style='padding-bottom:7px'>{_term(line)}</td></tr>")
    return f"<table cellspacing='0' width='100%'>{''.join(rows)}</table>"


class HudWindow(QWidget):
    """Jarvis's on-screen presence: a small arc-reactor orb. Calm when idle, rings turn while thinking,
    pulses while speaking, green while listening. Click to talk, drag to move."""

    SIZE = 84

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.state = State.IDLE
        self.phase = 0.0
        self.on_click: Callable[[], None] = lambda: None
        self._press: QPoint | None = None
        self._moved = False
        self._timer = QTimer(self, interval=33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.SIZE - 20, screen.top() + 20)

    def set_state(self, state: State) -> None:
        self.state = state
        self.setToolTip(STATE_LABELS.get(state, ""))

    def _tick(self) -> None:
        speed = {State.THINKING: 0.16, State.SPEAKING: 0.09, State.LISTENING: 0.06}.get(self.state, 0.02)
        self.phase += speed
        self.update()

    def paintEvent(self, _e) -> None:
        import math

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(STATE_COLORS.get(self.state, "#5B6B7F"))
        center = QRectF(self.rect()).center()
        pulse = 0.5 + 0.5 * math.sin(self.phase * 3) if self.state is State.SPEAKING else 0.3
        glow = QColor(c)
        glow.setAlpha(int(40 + 60 * pulse))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        r = self.SIZE / 2 - 2
        p.drawEllipse(center, r, r)
        p.setBrush(QColor(12, 16, 24, 230))
        p.drawEllipse(center, r - 6, r - 6)
        pen = QPen(c, 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i, (radius, span) in enumerate(((r - 12, 100), (r - 20, 70))):
            p.setPen(pen)
            start = int(((self.phase * (1 if i == 0 else -1.4)) * 180 / math.pi + i * 90) * 16)
            box = QRectF(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius)
            p.drawArc(box, start, span * 16)
            p.drawArc(box, start + 180 * 16, span * 16)
        core = QColor(c)
        core.setAlpha(255)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(core)
        p.drawEllipse(center, 6 + 3 * pulse, 6 + 3 * pulse)

    def mousePressEvent(self, e) -> None:
        self._press = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        self._moved = False

    def mouseMoveEvent(self, e) -> None:
        if self._press is not None:
            self.move(e.globalPosition().toPoint() - self._press)
            self._moved = True

    def mouseReleaseEvent(self, _e) -> None:
        if self._press is not None and not self._moved:
            self.on_click()
        self._press = None


class ChatWindow(QWidget):
    """Text chat with Jarvis: history of spoken and typed exchanges, input line, repeat aloud, copy."""

    MAX = 200

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Джарвис — чат")
        self.setWindowIcon(state_icon(State.IDLE))
        self.resize(520, 600)
        self.on_send: Callable[[str], None] = lambda _t: None
        self.on_repeat: Callable[[], None] = lambda: None
        self.history: list[tuple[str, str]] = []
        self.setStyleSheet("""
            QWidget { background: #141821; color: #E8ECF3; font: 10.5pt 'Segoe UI'; }
            QTextBrowser { border: none; background: #141821; }
            QLineEdit { background: #1E2430; border: 1px solid #2C3442; border-radius: 8px; padding: 7px 10px; }
            QPushButton { background: #2458B8; border: none; border-radius: 8px; padding: 7px 14px; color: white; }
            QPushButton#ghost { background: #232A37; color: #C9D2E0; }
        """)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.input = QLineEdit(placeholderText="Вопрос или команда — Enter")
        send = QPushButton("Отправить")
        repeat = QPushButton("Повторить вслух", objectName="ghost")
        copy_btn = QPushButton("Копировать ответ", objectName="ghost")
        send.clicked.connect(self._send)
        self.input.returnPressed.connect(self._send)
        repeat.clicked.connect(lambda: self.on_repeat())
        copy_btn.clicked.connect(self._copy_last)
        row = QHBoxLayout()
        row.addWidget(self.input, 1)
        row.addWidget(send)
        tools = QHBoxLayout()
        tools.addWidget(repeat)
        tools.addWidget(copy_btn)
        tools.addStretch(1)
        root = QVBoxLayout(self)
        root.addWidget(self.view, 1)
        root.addLayout(tools)
        root.addLayout(row)

    def add(self, role: str, text: str) -> None:
        self.history = (self.history + [(role, text)])[-self.MAX:]
        who, color = ("Вы", "#8FA3BF") if role == "user" else ("Джарвис", "#6D9BF0")
        body = html.escape(text).replace("\n", "<br>")
        self.view.append(f"<p style='margin:6px 0'><b style='color:{color}'>{who}</b><br>{body}</p>")
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def _send(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.on_send(text)

    def _copy_last(self) -> None:
        last = next((t for r, t in reversed(self.history) if r == "assistant"), "")
        if last:
            QApplication.clipboard().setText(last)

    def open(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()


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
    _chat = Signal(str, str)
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
        self.chat = ChatWindow()
        self._chat.connect(self.chat.add)
        self.hud: HudWindow | None = None

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

    def chat_add(self, role: str, text: str) -> None:
        self._chat.emit(role, text)

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
        if a.cfg.ui.hud:
            self.hud = HudWindow()
            self.hud.on_click = lambda: a._threadsafe(a.listen_now)
            self.hud.show()
        self.window.on_user_nav = lambda i: a._threadsafe(a.deck_paged, i)
        self.window.on_user_close = lambda: a._threadsafe(a.deck_closed)
        menu = QMenu()
        menu.addAction(self._status_action)
        menu.addSeparator()
        listen = menu.addAction("Слушать сейчас  (Ctrl+Alt+J)")
        listen.triggered.connect(lambda: a._threadsafe(a.listen_now))
        self._mute_action.triggered.connect(lambda: a._threadsafe(a.toggle_mute))
        menu.addAction(self._mute_action)
        self.chat.on_send = lambda text: a.submit(a.handle_text(text))
        self.chat.on_repeat = lambda: a.submit(a.speaker.say(a.speaker.last_text)) if a.speaker.last_text else None
        ask = menu.addAction("Чат с Джарвисом…")
        ask.triggered.connect(self.chat.open)
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

    def _on_state(self, state: State, detail: str) -> None:
        if self.hud is not None:
            self.hud.set_state(state)
        label = STATE_LABELS.get(state, state.value)
        self.tray.setIcon(state_icon(state))
        tip = f"Джарвис — {label}" + (f"\n{detail[:80]}" if detail else "")
        self.tray.setToolTip(tip)
        self._status_action.setText(label)
        self._mute_action.setText("Включить микрофон" if state is State.MUTED else "Выключить микрофон")
