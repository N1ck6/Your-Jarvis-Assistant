"""The Jarvis icon (assistant/ui/icon.svg) in the colors of his state: tray, windows and the browser extension.

idle — blue, listening — cyan, thinking — amber, speaking — bright blue, muted — grey with a slash, error — red.
The extension's PNGs are made by scripts/make_icons.py from the same picture.
"""
from __future__ import annotations

from pathlib import Path

from assistant.core import State

SVG = Path(__file__).with_name("icon.svg")

# (accent, glow, deep): replace #2AA8E0, #8FDCFF, #0B3A5C of the picture.
PALETTES: dict[str, tuple[str, str, str]] = {
    "idle": ("#2AA8E0", "#8FDCFF", "#0B3A5C"),
    "listening": ("#1FD1A5", "#9BFFE4", "#0A4A3E"),
    "thinking": ("#F0A92E", "#FFE09A", "#5A3A0A"),
    "speaking": ("#3F8CFF", "#B5D3FF", "#10306A"),
    "muted": ("#6E7A88", "#AEB8C4", "#2A323C"),
    "error": ("#E0503C", "#FFA393", "#5A1A12"),
    "off": ("#5E6873", "#9AA3AD", "#252B32"),
}
_STATE_KEY = {State.IDLE: "idle", State.LISTENING: "listening", State.THINKING: "thinking",
              State.SPEAKING: "speaking", State.MUTED: "muted", State.ERROR: "error"}


def palette_key(state: State | None) -> str:
    return _STATE_KEY.get(state, "off") if state is not None else "off"


# At 16–24 px the fine segments and the inner triangle turn to mush: a bolder version of the same picture.
SMALL_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128">
  <defs><radialGradient id="core" cx="50%" cy="38%" r="62%"><stop offset="0" stop-color="#8FDCFF"/>
  <stop offset="0.6" stop-color="#2AA8E0"/><stop offset="1" stop-color="#0B3A5C"/></radialGradient></defs>
  <circle cx="64" cy="64" r="62" fill="#04101C"/>
  <circle cx="64" cy="64" r="52" fill="none" stroke="#2AA8E0" stroke-width="14" stroke-dasharray="20 7"
          transform="rotate(-80 64 64)"/>
  <circle cx="64" cy="64" r="40" fill="url(#core)"/>
  <path d="M28 44 H100 L64 104 Z" fill="#0B3A5C" stroke="#04101C" stroke-width="7" stroke-linejoin="round"/>
  <path d="M28 44 H100 L64 104 Z" fill="none" stroke="#8FDCFF" stroke-width="7" stroke-linejoin="round"/>
</svg>"""


def svg_for(key: str, small: bool = False) -> str:
    accent, glow, deep = PALETTES.get(key, PALETTES["idle"])
    text = SMALL_SVG if small else SVG.read_text(encoding="utf-8")
    text = text.replace("#2AA8E0", accent).replace("#8FDCFF", glow).replace("#0B3A5C", deep)
    if key == "muted":
        slash = '<path d="M24 104 L104 24" stroke="#04101C" stroke-width="16" stroke-linecap="round"/>' \
                '<path d="M24 104 L104 24" stroke="#E0503C" stroke-width="9" stroke-linecap="round"/>'
        text = text.replace("</svg>", slash + "</svg>")
    return text


def render(key: str, size: int):
    """QImage of the icon in a palette (needs a Qt application)."""
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg_for(key, small=size <= 32).encode("utf-8"))).render(painter)
    painter.end()
    return image


def state_icon(state: State | None):
    """QIcon for the tray: sharp at 16, 24, 32 and 64 px."""
    from PySide6.QtGui import QIcon, QPixmap

    icon = QIcon()
    key = palette_key(state)
    for size in (16, 20, 24, 32, 48, 64):
        icon.addPixmap(QPixmap.fromImage(render(key, size)))
    return icon
