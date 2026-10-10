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

from PySide6.QtCore import (QBuffer, QByteArray, QEasingCurve, QIODevice, QObject, QPoint, QPointF, QPropertyAnimation, QRect,
                            QRectF, Qt, QTimer, Signal)
from PySide6.QtGui import (QAction, QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap,
                           QTextDocument)
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


_ICONS: dict[State | None, QIcon] = {}


def state_icon(state: State | None) -> QIcon:
    """The arc-reactor icon (assistant/ui/icon.svg) in the colors of the state."""
    if state not in _ICONS:
        from assistant.ui import icons

        _ICONS[state] = icons.state_icon(state)
    return _ICONS[state]


class Panels:
    """Floating windows (cards, picture) sit near the middle of the screen side by side with a gap, never on top of
    each other. A window the user dragged stays where it was put."""

    GAP = 16

    def __init__(self) -> None:
        self.windows: list[QWidget] = []

    def add(self, window: QWidget) -> None:
        self.windows.append(window)

    def arrange(self) -> None:
        visible = [w for w in self.windows if w.isVisible()]
        shown = [w for w in visible if getattr(w, "_user_pos", None) is None]
        pinned = [w for w in visible if getattr(w, "_user_pos", None) is not None]
        if not shown:
            return
        screen = QApplication.primaryScreen().availableGeometry()
        if pinned:
            self._beside(shown, pinned[0].frameGeometry(), screen)
            return
        total = sum(w.width() for w in shown) + self.GAP * (len(shown) - 1)
        # The top edge stays put while cards of different height come and go: no jumping window.
        top = screen.top() + int(screen.height() * 0.18)
        if total > screen.width() - 24:  # a narrow screen: one under the other
            y = screen.top() + 12
            for w in shown:
                w.move(screen.center().x() - w.width() // 2, y)
                y += w.height() + self.GAP
            return
        x = max(screen.left() + 12, screen.center().x() - total // 2)
        for w in shown:
            y = max(screen.top() + 8, min(top, screen.bottom() - w.height() - 8))
            w.move(x, y)
            x += w.width() + self.GAP

    def _beside(self, shown: list[QWidget], anchor: QRect, screen: QRect) -> None:
        """The user moved one window: the others line up next to it (the side with room), not on top of it."""
        total = sum(w.width() for w in shown) + self.GAP * len(shown)
        if anchor.left() - screen.left() >= total:
            x = anchor.left() - total
        elif screen.right() - anchor.right() >= total:
            x = anchor.right() + 1 + self.GAP
        else:  # no room at either side: under or above it
            y = anchor.bottom() + self.GAP if screen.bottom() - anchor.bottom() > anchor.top() - screen.top() else None
            for w in shown:
                ty = y if y is not None else anchor.top() - self.GAP - w.height()
                w.move(min(max(anchor.left(), screen.left()), screen.right() - w.width()),
                       max(screen.top(), min(ty, screen.bottom() - w.height())))
            return
        for w in shown:
            y = max(screen.top() + 8, min(anchor.top(), screen.bottom() - w.height() - 8))
            w.move(x, y)
            x += w.width() + self.GAP


PANELS = Panels()


def _caps(title: str) -> str:
    """Window headers are in capitals, but a formula keeps its case: "График y = x² − 3x", "C₂H₄"."""
    return title if re.search(r"[=²³√₀-₉]|\b[a-zA-Z]\b", title) else title.upper()


class GlassWindow(QWidget):
    """Frameless rounded dark window: drag to move, Esc closes."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._drag: QPoint | None = None
        self._user_pos: QPoint | None = None  # where the user dragged the window (kept until restart)

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

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)
            self._user_pos = self.pos()

    def mouseReleaseEvent(self, _e) -> None:
        self._drag = None

    def _clamp_to_user_pos(self) -> None:
        screen = (QGuiApplication.screenAt(self._user_pos) or QApplication.primaryScreen()).availableGeometry()
        x = min(max(self._user_pos.x(), screen.left()), screen.right() - self.width())
        y = min(max(self._user_pos.y(), screen.top()), screen.bottom() - self.height())
        self.move(x, y)


ELEMENT_COLORS = {"C": "#E8ECF3", "H": "#8FA3BF", "O": "#F06A6A", "N": "#6D9BF0", "S": "#E8C547", "P": "#F0A04B",
                  "Cl": "#5FD38A", "F": "#5FD38A", "Br": "#C46A4A", "I": "#B07FE0"}


def draw_plot(spec: dict, w: int, h: int) -> QPixmap:
    """A function graph in the window style: grid, axes with ticks, the curve broken at asymptotes."""
    import math

    from assistant.visuals import parse_formula

    pm = QPixmap(w, h)
    pm.fill(QColor("#0F131B"))
    f = parse_formula(str(spec.get("expr", "")))
    if f is None:
        return pm
    x0, x1 = (float(v) for v in spec.get("x", (-6, 6)))
    n = 800
    xs = [x0 + (x1 - x0) * i / (n - 1) for i in range(n)]
    ys = [f(x) for x in xs]
    finite = sorted(y for y in ys if math.isfinite(y))
    if not finite:
        return pm
    lo, hi = finite[int(len(finite) * 0.04)], finite[int(len(finite) * 0.96) - 1]
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    lo, hi = lo - span * 0.12, hi + span * 0.12
    if 0 < lo < span:      # keep the x axis in view when the curve is just above it
        lo = -span * 0.08
    if -span < hi < 0:
        hi = span * 0.08
    m = 34
    sx = lambda x: m + (x - x0) / (x1 - x0) * (w - 2 * m)
    sy = lambda y: h - m - (y - lo) / (hi - lo) * (h - 2 * m)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont("Segoe UI", 8)
    p.setFont(font)

    def step(span: float) -> float:
        raw = span / 8
        mag = 10 ** math.floor(math.log10(raw))
        return next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)

    for axis_lo, axis_hi, vertical in ((x0, x1, True), (lo, hi, False)):
        st = step(axis_hi - axis_lo)
        v = math.ceil(axis_lo / st) * st
        while v <= axis_hi + 1e-9:
            p.setPen(QPen(QColor(255, 255, 255, 16), 1))
            label = f"{v:g}" if abs(v) > 1e-9 else "0"
            if vertical:
                p.drawLine(int(sx(v)), m, int(sx(v)), h - m)
                p.setPen(QColor("#7D8BA0"))
                p.drawText(int(sx(v)) - 14, h - m + 4, 28, 14, Qt.AlignmentFlag.AlignHCenter, label)
            else:
                p.drawLine(m, int(sy(v)), w - m, int(sy(v)))
                p.setPen(QColor("#7D8BA0"))
                p.drawText(0, int(sy(v)) - 7, m - 4, 14, Qt.AlignmentFlag.AlignRight, label)
            v += st
    p.setPen(QPen(QColor(255, 255, 255, 90), 1.4))
    if lo <= 0 <= hi:
        p.drawLine(m, int(sy(0)), w - m, int(sy(0)))
    if x0 <= 0 <= x1:
        p.drawLine(int(sx(0)), m, int(sx(0)), h - m)
    pen = QPen(QColor("#3F9BFF"), 2.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setClipRect(m, m, w - 2 * m, h - 2 * m)
    path = QPainterPath()
    prev = None
    jump = (hi - lo) * 1.5
    for x, y in zip(xs, ys):
        if not math.isfinite(y) or (prev is not None and abs(y - prev) > jump):
            prev = None if not math.isfinite(y) else y
            if math.isfinite(y):
                path.moveTo(sx(x), sy(y))
            continue
        path.lineTo(sx(x), sy(y)) if prev is not None else path.moveTo(sx(x), sy(y))
        prev = y
    p.drawPath(path)
    p.setClipping(False)
    font = QFont("Segoe UI", 11, QFont.Weight.DemiBold)
    p.setFont(font)
    label = str(spec.get("label", ""))
    box = p.fontMetrics().boundingRect(label).adjusted(-8, -4, 8, 4)
    box.moveTopLeft(QPoint(m + 4, m))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(15, 19, 27, 225))  # the curve may pass under the label
    p.drawRoundedRect(QRectF(box), 6, 6)
    p.setPen(QColor("#FFFFFF"))
    p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
    p.end()
    return pm


def draw_molecule(mol: dict, w: int, h: int) -> QPixmap:
    """Structural formula: element letters in colour, single / double / triple bonds as 1-3 lines."""
    import math

    pm = QPixmap(w, h)
    pm.fill(QColor("#0F131B"))
    atoms, bonds = mol.get("atoms") or [], mol.get("bonds") or []
    if not atoms:
        return pm
    xs, ys = [a[1] for a in atoms], [a[2] for a in atoms]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    scale = min((w - 70) / max(max(xs) - min(xs), 1.0), (h - 70) / max(max(ys) - min(ys), 1.0), 70.0)
    scale = min(scale, (min(w, h) - 70) / span * 1.6)
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    pos = [QPointF(w / 2 + (a[1] - cx) * scale, h / 2 - (a[2] - cy) * scale) for a in atoms]
    explicit = bool(mol.get("explicit"))
    labelled = [explicit or a[0] != "C" for a in atoms]
    radius = max(7.0, min(13.0, scale * 0.22))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    for i, j, order in bonds:
        a, b = pos[i], pos[j]
        dx, dy = b.x() - a.x(), b.y() - a.y()
        d = math.hypot(dx, dy) or 1.0
        ux, uy = dx / d, dy / d
        # Lines start outside the atom letters.
        a2 = QPointF(a.x() + ux * (radius if labelled[i] else 0), a.y() + uy * (radius if labelled[i] else 0))
        b2 = QPointF(b.x() - ux * (radius if labelled[j] else 0), b.y() - uy * (radius if labelled[j] else 0))
        nx, ny = -uy, ux
        gap = max(3.0, scale * 0.07)
        offsets = {1: [0.0], 2: [-gap / 2, gap / 2], 3: [-gap, 0.0, gap]}.get(int(order), [0.0])
        p.setPen(QPen(QColor("#C9D2E0"), 2.0 if scale > 30 else 1.5))
        for o in offsets:
            p.drawLine(QPointF(a2.x() + nx * o, a2.y() + ny * o), QPointF(b2.x() + nx * o, b2.y() + ny * o))
    font = QFont("Segoe UI", int(max(9, min(15, scale * 0.26))), QFont.Weight.DemiBold)
    p.setFont(font)
    for k, (sym, _x, _y) in enumerate(atoms):
        if not labelled[k]:
            continue
        p.setPen(QColor(ELEMENT_COLORS.get(sym, "#E8ECF3")))
        box = QRectF(pos[k].x() - radius * 1.6, pos[k].y() - radius, radius * 3.2, radius * 2)
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, sym)
    p.end()
    return pm


class VisualWindow(GlassWindow):
    """A picture next to the explanation: function graph, structural formula or photo, with a caption."""

    WIDTH = 460
    MARGIN = 20

    def __init__(self, linger_sec: float) -> None:
        super().__init__()
        self.setFixedWidth(self.WIDTH)
        self.setStyleSheet("""
            QLabel { color: #E8ECF3; background: transparent; }
            QLabel#title { color: #8FA3BF; font: 600 10pt 'Segoe UI'; letter-spacing: 1px; }
            QLabel#caption { font: 10.5pt 'Segoe UI'; color: #D6DCE6; }
            QLabel#source { color: #7D8BA0; font: 8.5pt 'Segoe UI'; }
            QLabel#picture { background: #0F131B; border-radius: 12px; }
            QLabel#picture[light="true"] { background: #F5F5F5; }
            QPushButton { background: rgba(255,255,255,0.08); color: #E8ECF3; border: none; border-radius: 8px;
                          padding: 4px 10px; font: 11pt 'Segoe UI'; }
            QPushButton:hover { background: rgba(255,255,255,0.18); }
        """)
        self.title = QLabel(objectName="title")
        self.picture = QLabel(objectName="picture", alignment=Qt.AlignmentFlag.AlignCenter)
        self.caption = QLabel(objectName="caption", wordWrap=True)
        self.source = QLabel(objectName="source")
        close_btn = QPushButton("✕")
        close_btn.clicked.connect(self.fade_out)
        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(close_btn)
        root = QVBoxLayout(self)
        root.setContentsMargins(self.MARGIN, 16, self.MARGIN, 14)
        root.setSpacing(10)
        root.addLayout(top)
        root.addWidget(self.picture)
        root.addWidget(self.caption)
        root.addWidget(self.source)
        self._close_timer = QTimer(self, singleShot=True, interval=int(max(linger_sec, 40) * 1000))
        self._close_timer.timeout.connect(self.fade_out)
        self.win_anim = QPropertyAnimation(self, b"windowOpacity", self, duration=220)
        self.win_anim.finished.connect(self._faded)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.fade_out()

    def show_visual(self, visual) -> None:
        inner = self.WIDTH - 2 * self.MARGIN
        if visual.plot:
            pm = draw_plot(visual.plot, inner, 330)
        elif visual.molecule:
            pm = draw_molecule(visual.molecule, inner, 320)
        else:
            pm = QPixmap()
            pm.loadFromData(visual.image)
            if pm.isNull():
                return
            pm = pm.scaled(inner - (16 if visual.light else 0), 380, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self.picture.setProperty("light", bool(visual.light))
        self.picture.style().unpolish(self.picture)
        self.picture.style().polish(self.picture)
        self.picture.setPixmap(_rounded(pm, 12))
        # Formulas keep their case (C₂H₄, y = x²); words get the window's small caps style.
        title = "ГРАФИК" if visual.plot else visual.title if visual.molecule else visual.title.upper()
        self.title.setText(html.escape(title))
        self.caption.setText(visual.caption)
        self.caption.setVisible(bool(visual.caption))
        self.source.setText(f"Источник: {visual.source}" if visual.source else "")
        self.source.setVisible(bool(visual.source))
        self.win_anim.stop()
        self.setWindowOpacity(1.0)
        self.show()
        self.layout().activate()
        self.adjustSize()  # (resize() does not shrink a frameless tool window here)
        if self._user_pos is not None:
            self._clamp_to_user_pos()
        PANELS.arrange()
        self._close_timer.start()

    def fade_out(self) -> None:
        self._close_timer.stop()
        if not self.isVisible():
            return
        self.win_anim.stop()
        self.win_anim.setStartValue(self.windowOpacity())
        self.win_anim.setEndValue(0.0)
        self.win_anim.start()

    def _faded(self) -> None:
        if self.windowOpacity() < 0.05:
            self.hide()
            PANELS.arrange()


def _rounded(pm: QPixmap, radius: int) -> QPixmap:
    out = QPixmap(pm.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(pm.rect()), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


class DeckWindow(GlassWindow):
    """Frameless glass card window near the middle of the screen (or wherever the user dragged it)."""

    WIDTH = 540
    MARGIN = 22

    def __init__(self, linger_sec: float) -> None:
        super().__init__()
        self.setFixedWidth(self.WIDTH)
        self.deck: Deck | None = None
        self.index = 0
        self.default_linger = linger_sec
        self._close_timer = QTimer(self, singleShot=True, interval=int(linger_sec * 1000))
        self._close_timer.timeout.connect(self._time_up)
        self._timed_out = False
        # Set by QtUi: the user paged by hand / closed the window (the core speaks or stops).
        self.on_user_nav: Callable[[int], None] = lambda _i: None
        self.on_user_close: Callable[[], None] = lambda: None
        # Read-time is over: the picture of the same answer closes with the card (not when a new answer
        # replaces the card: the new answer may have its own picture).
        self.on_hidden: Callable[[], None] = lambda: None

        self.setStyleSheet("""
            QLabel { color: #E8ECF3; background: transparent; }
            QLabel#title { color: #8FA3BF; font: 600 10pt 'Segoe UI'; letter-spacing: 1px; }
            QLabel#heading { font: 600 15pt 'Segoe UI'; color: #FFFFFF; }
            QLabel#body { font: 11pt 'Segoe UI'; color: #D6DCE6; }
            QLabel#counter { color: #7D8BA0; font: 9pt 'Segoe UI'; }
            QLabel#pin { color: #D9A62E; font: 9pt 'Segoe UI'; }
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
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                          | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.body.setOpenExternalLinks(True)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.counter = QLabel(objectName="counter")
        self.prev_btn, self.next_btn, close_btn = QPushButton("‹"), QPushButton("›"), QPushButton("✕")
        close_btn.setToolTip("Закрыть")
        self.pin_mark = QLabel("закреплено", objectName="pin")
        self.pin_mark.setToolTip("Эта карточка не исчезнет сама — закройте её крестиком")
        self.pin_mark.hide()
        self.copy_btn = QPushButton("Копировать")
        self.copy_btn.setToolTip("Скопировать текст карточки")
        self.prev_btn.clicked.connect(lambda: self._user_nav(self.index - 1))
        self.next_btn.clicked.connect(lambda: self._user_nav(self.index + 1))
        close_btn.clicked.connect(self._user_close)
        self.copy_btn.clicked.connect(self._copy)

        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(self.pin_mark)
        top.addWidget(close_btn)
        bottom = QHBoxLayout()
        bottom.addWidget(self.counter, 1)
        bottom.addWidget(self.copy_btn)
        bottom.addWidget(self.prev_btn)
        bottom.addWidget(self.next_btn)
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

    def _time_up(self) -> None:
        self._timed_out = True
        self.fade_out()

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
            QApplication.clipboard().setText((card.heading + "\n" if card.heading else "") + card_plain(card.screen))
            self.copy_btn.setText("Скопировано")
            QTimer.singleShot(1500, lambda: self.copy_btn.setText("Копировать"))

    def _fit(self) -> None:
        """The text area grows with the card up to ~60% of the screen, then scrolls."""
        screen = (QGuiApplication.screenAt(self.pos()) or QApplication.primaryScreen()).availableGeometry()
        width = self.WIDTH - 2 * self.MARGIN - 8
        # Measured on a document of exactly this width: the label's own estimate kept the height of the
        # previous, longer card (a short answer sat in a half-empty window).
        doc = QTextDocument()
        doc.setDefaultFont(self.body.font())
        doc.setDocumentMargin(0)
        doc.setHtml(self.body.text())
        doc.setTextWidth(width)
        need = int(doc.size().height()) + 8
        self.scroll.setFixedHeight(max(40, min(need, int(screen.height() * 0.6))))

    def _place(self) -> None:
        """Near the middle of the screen, next to the picture window; after the user drags it, keep their spot."""
        self._fit()
        self._shrink()
        # The label's new size hint arrives with the next layout pass: fit once more right after it.
        QTimer.singleShot(0, self._shrink)

    def _shrink(self) -> None:
        self.layout().activate()
        self.adjustSize()  # a shorter card shrinks the window (resize() does not shrink it here)
        if self._user_pos is not None:
            self._clamp_to_user_pos()
        PANELS.arrange()

    def set_deck(self, deck: Deck) -> None:
        self.deck = copy.deepcopy(deck)
        self.title.setText(html.escape(_caps(deck.title)))
        self._close_timer.stop()
        self._timed_out = False
        self.pin_mark.setVisible(deck.pinned)
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
            self._start_close()

    def _start_close(self) -> None:
        """The read-time countdown; a pinned card has none (only ✕ closes it)."""
        if self.deck is not None and self.deck.pinned:
            self._close_timer.stop()
            return
        linger = self.deck.linger if self.deck is not None and self.deck.linger > 0 else self.default_linger
        self._close_timer.setInterval(int(linger * 1000))
        self._close_timer.start()

    def update_deck(self, deck: Deck) -> None:
        if self.deck is None:
            return self.set_deck(deck)
        self.deck = copy.deepcopy(deck)
        self.title.setText(html.escape(_caps(deck.title)))
        self._update_counter()
        if deck.done:
            self._start_close()

    def _update_counter(self) -> None:
        many = bool(self.deck and len(self.deck.cards) > 1)
        if many:
            self.counter.setText(f"{self.index + 1} / {len(self.deck.cards)}{'' if self.deck.done else ' …'}")
        else:
            self.counter.setText("")
        # One card (an answer, a list): arrows have nothing to page.
        self.prev_btn.setVisible(many)
        self.next_btn.setVisible(many)

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
            self._start_close()

    def fade_out(self) -> None:
        self._close_timer.stop()
        self.win_anim.stop()
        self.win_anim.setStartValue(self.windowOpacity())
        self.win_anim.setEndValue(0.0)
        self.win_anim.start()

    def _faded(self) -> None:
        if self.windowOpacity() < 0.05:  # a new deck may have stopped the fade halfway
            self.hide()
            if self._timed_out:
                self.on_hidden()
            self._timed_out = False
            PANELS.arrange()


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
_BULLET = re.compile(r"^(?:[-•–]|\*(?=\s)|\+(?=\s))\s*(.+)$")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
# **bold** / __bold__, *italic* / _italic_, `code`, [text](link)
_INLINE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1|(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"
                     r"|(?<!\w)_(?=\S)([^_]+?)(?<=\S)_(?!\w)|`([^`]+)`|\[([^\]]+)\]\((https?://[^)\s]+)\)")
_CODE_STYLE = "font-family:'Cascadia Mono',Consolas,monospace;font-size:10pt;color:#9FD3A6"


def inline_md(text: str) -> str:
    """Escapes HTML and renders inline markdown: models write **bold** even when told not to."""
    out: list[str] = []
    pos = 0
    plain = lambda t: html.escape(t.replace("**", "").replace("__", ""))  # an unpaired marker is noise
    for m in _INLINE.finditer(text):
        out.append(plain(text[pos:m.start()]))
        if m.group(2) is not None:
            out.append(f"<b style='color:#FFFFFF'>{inline_md(m.group(2))}</b>")
        elif m.group(3) is not None or m.group(4) is not None:
            out.append(f"<i>{inline_md(m.group(3) or m.group(4))}</i>")
        elif m.group(5) is not None:
            out.append(f"<span style=\"{_CODE_STYLE}\">{html.escape(m.group(5))}</span>")
        else:
            out.append(f"<a style='color:#6D9BF0' href='{html.escape(m.group(7), quote=True)}'>{html.escape(m.group(6))}</a>")
        pos = m.end()
    out.append(plain(text[pos:]))
    return "".join(out)


def _term(text: str) -> str:
    """'Термин — пояснение' -> bold term."""
    m = _TERM.match(text)
    if m and len(m.group(1).split()) <= 5 and "*" not in m.group(1):
        return f"<b style='color:#FFFFFF'>{inline_md(m.group(1))}</b> — {inline_md(m.group(2))}"
    return inline_md(text)


_LANG_TAG = re.compile(r"(?i)(python|py|bash|sh|shell|cmd|powershell|ps1|js|javascript|typescript|ts|sql|json|yaml|"
                       r"c|cpp|c\+\+|c#|csharp|java|kotlin|go|rust|php|ruby|html|css|text|plaintext)")
_ART = re.compile(r"[\s|/\\]+")   # "||", "| \", "/" lines: a molecule or a diagram drawn with characters


def _is_code_line(line: str) -> bool:
    s = line.strip()
    return s.startswith(">") or (len(s) > 2 and s.startswith("`") and s.endswith("`") and s.count("`") == 2)


def _code_text(line: str) -> str:
    s = line.lstrip()
    if s.startswith(">"):
        s = s[1:]
        return (s[1:] if s.startswith(" ") else s).rstrip()  # "> " then the code's own indentation
    return s.strip()[1:-1]


def _clean_code(lines: list[str]) -> list[str]:
    """A leading "python" tag and blank edges go; a drawing made of | / \\ is dropped entirely: pictures are shown
    in their own window, ASCII art only confuses."""
    while lines and not lines[0].strip():
        lines = lines[1:]
    if lines and _LANG_TAG.fullmatch(lines[0].strip()):
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    if sum(1 for ln in lines if ln.strip() and _ART.fullmatch(ln)) >= 2:
        return []
    return lines


def _parse_card(text: str) -> list[tuple[str, object]]:
    """Card text -> items: ("code", lines), ("table", lines), ("heading", s), ("bullet", s), ("step", (n, s)),
    ("para", s). The explanation format ('- ', '1. ', '> ') and the markdown models write anyway both parse."""
    from assistant.mathtext import to_display

    items: list[tuple[str, object]] = []
    lines = to_display(text).splitlines()  # "$x^2$" -> "x²": no raw LaTeX on a card
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or re.fullmatch(r"[-*_#=]{3,}", line) or _ART.fullmatch(line):
            continue
        if line.startswith("```") or _is_code_line(line):
            if line.startswith("```"):
                code = [line[3:].strip()]  # "```python": the tag is dropped by _clean_code
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code.append(lines[i].rstrip())
                    i += 1
                i += 1
            else:  # consecutive "> line" / "`line`" rows are one listing, not a box per line
                code = [_code_text(lines[i - 1])]
                while i < len(lines) and _is_code_line(lines[i]):
                    code.append(_code_text(lines[i]))
                    i += 1
            code = _clean_code(code)
            if code:
                items.append(("code", code))
            elif items and str(items[-1][1]).rstrip().endswith(":"):
                items.pop()  # "Структурная формула:" whose drawing was dropped
        elif line.startswith("|") and line.count("|") >= 2:
            block = [line]
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            items.append(("table", block))
        elif m := _HEADING.match(line):
            items.append(("heading", m.group(1)))
        elif m := _BULLET.match(line):
            items.append(("bullet", m.group(1).strip()))
        elif m := _STEP.match(line):
            items.append(("step", (m.group(1), m.group(2))))
        else:
            items.append(("para", line))
    return items


def _table(lines: list[str]) -> str:
    """Markdown table rows ("| a | b |"); the "|---|" separator is dropped."""
    rows = []
    for i, line in enumerate(lines):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        tag = "b" if i == 0 else "span"
        rows.append("<tr>" + "".join(f"<td style='padding:3px 10px 3px 0'><{tag}>{inline_md(c)}</{tag}></td>"
                                     for c in cells) + "</tr>")
    return f"<table cellspacing='0' style='margin-bottom:8px'>{''.join(rows)}</table>"


def card_plain(text: str) -> str:
    """What "Копировать" puts on the clipboard: the code as code, formulas as shown, no markdown marks."""
    out: list[str] = []
    for kind, data in _parse_card(text):
        if kind == "code":
            out.extend(data)  # type: ignore[arg-type]
        elif kind == "table":
            out.extend(re.sub(r"\*\*|__|`", "", ln) for ln in data)  # type: ignore[union-attr]
        elif kind == "bullet":
            out.append("- " + re.sub(r"\*\*|__|`", "", str(data)))
        elif kind == "step":
            out.append(f"{data[0]}. " + re.sub(r"\*\*|__|`", "", data[1]))  # type: ignore[index]
        else:
            out.append(re.sub(r"\*\*|__|`", "", str(data)))
    return "\n".join(out).strip()


def render_screen(text: str) -> str:
    """Card text -> Qt rich text, never with raw marks. Code and tables are separate blocks: inside the bullet table
    a wide listing used to push the bullets to the right."""
    parts: list[str] = []
    rows: list[str] = []

    def flush() -> None:
        if rows:
            parts.append(f"<table cellspacing='0' width='100%'>{''.join(rows)}</table>")
            rows.clear()

    for kind, data in _parse_card(text):
        if kind == "code":
            flush()
            code = "<br>".join(html.escape(ln) for ln in data)  # type: ignore[union-attr]
            parts.append(f"<table cellpadding='8' width='100%' style='background-color:#0F131B;margin:2px 0 10px 0'>"
                         f"<tr><td style=\"{_CODE_STYLE};white-space:pre-wrap\">{code}</td></tr></table>")
        elif kind == "table":
            flush()
            parts.append(_table(data))  # type: ignore[arg-type]
        elif kind == "heading":
            rows.append(f"<tr><td colspan='2' style='padding:4px 0 6px 0'><b style='color:#FFFFFF;font-size:12pt'>"
                        f"{inline_md(str(data))}</b></td></tr>")
        elif kind == "bullet":
            rows.append(f"<tr><td width='18' style='color:#3F7FE0'>●</td>"
                        f"<td style='padding-bottom:7px'>{_term(str(data))}</td></tr>")
        elif kind == "step":
            rows.append(f"<tr><td width='18' style='color:#3F7FE0;font-weight:600'>{data[0]}</td>"  # type: ignore[index]
                        f"<td style='padding-bottom:7px'>{_term(data[1])}</td></tr>")  # type: ignore[index]
        else:
            rows.append(f"<tr><td colspan='2' style='padding-bottom:7px'>{_term(str(data))}</td></tr>")
    flush()
    return "".join(parts) or "<table></table>"


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
        body = html.escape(text) if role == "user" else render_screen(text)
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


SETTINGS_TITLE = "Настройки Джарвиса"   # <title> of the settings page


def _app_browser() -> str | None:
    """Edge (always on Windows 10/11) or Chrome: both open a page as a bare app window with --app."""
    import shutil
    from pathlib import Path

    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            if base and (Path(base) / rel).exists():
                return str(Path(base) / rel)
    return shutil.which("msedge") or shutil.which("chrome")


def open_settings_window(url: str) -> None:
    """Settings as a small window of their own, centred, without tabs or an address bar (not a browser tab).
    A window that is already open is brought to the front instead of opening a second one."""
    from assistant import winutil
    from assistant.paths import DATA_DIR

    try:
        win = next((w for w in winutil.list_windows() if w.title.startswith(SETTINGS_TITLE)), None)
        if win is not None:
            winutil.activate(win)
            return
    except Exception:
        log.debug("поиск окна настроек", exc_info=True)
    exe = _app_browser()
    if exe is None:
        webbrowser.open(url)
        return
    screen = QApplication.primaryScreen().availableGeometry()
    w, h = min(1000, screen.width() - 80), min(860, screen.height() - 60)
    x, y = screen.center().x() - w // 2, screen.center().y() - h // 2
    # Its own profile: the window opens at this size even when the browser is already running, and nothing
    # from the user's browser profile (extensions, tabs) gets mixed in.
    profile = DATA_DIR / "settings-window"
    subprocess.Popen([exe, f"--app={url}", f"--window-size={w},{h}", f"--window-position={x},{y}",
                      f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                      "--disable-extensions", "--disable-sync", "--disable-features=Translate"])


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
    _show_visual = Signal(object)
    _close_visual = Signal()
    _open_settings = Signal(str)
    quit_requested = Signal()

    def __init__(self, linger_sec: float) -> None:
        super().__init__()
        self.visual = VisualWindow(linger_sec)
        self.window = DeckWindow(linger_sec)
        PANELS.add(self.visual)   # picture on the left, text on the right
        PANELS.add(self.window)
        self.window.on_hidden = self.visual.fade_out
        self._show_visual.connect(self.visual.show_visual)
        self._close_visual.connect(self.visual.fade_out)
        self._open_settings.connect(open_settings_window)
        self.tray = QSystemTrayIcon(state_icon(None))
        self.tray.setToolTip("Джарвис — загрузка…")
        self.assistant: Assistant | None = None
        self._status_action = QAction("Загрузка…")
        self._status_action.setEnabled(False)
        self._mute_action = QAction("Выключить микрофон")
        self._state.connect(self._on_state)
        self._notify.connect(lambda t, m: self.tray.showMessage(t, m, state_icon(State.SPEAKING), 8000))
        self._linger = linger_sec
        self.pinned: list[DeckWindow] = []
        self._show_deck.connect(self._route_deck)
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

    def _route_deck(self, deck: Deck) -> None:
        """Ordinary cards share one window (the next answer replaces the last one); a pinned card gets a window of
        its own that stays until ✕."""
        if not deck.pinned:
            self.window.set_deck(deck)
            return
        win = DeckWindow(self._linger)
        win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.pinned.append(win)
        PANELS.add(win)

        def closed(w: DeckWindow = win) -> None:
            if w in self.pinned:
                self.pinned.remove(w)
            if w in PANELS.windows:
                PANELS.windows.remove(w)
            w.close()
            PANELS.arrange()
        win.on_user_close = lambda: QTimer.singleShot(260, closed)  # after the fade-out
        win.set_deck(deck)

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

    def show_visual(self, visual) -> None:
        self._show_visual.emit(visual)

    def close_visual(self) -> None:
        self._close_visual.emit()

    def open_settings(self, url: str) -> None:
        self._open_settings.emit(url)

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
        self.window.on_user_close = lambda: (self.visual.fade_out(), a._threadsafe(a.deck_closed))
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
        settings.triggered.connect(lambda: open_settings_window(a.voicelab_url))
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
