"""Icons from assistant/ui/icon.svg: browser_extension/icons/<state>-<size>.png and assistant/ui/jarvis.ico.

    .venv\\Scripts\\python scripts\\make_icons.py

Run after changing icon.svg or the palettes in assistant/ui/icons.py; the PNGs are kept in git.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QGuiApplication  # noqa: E402

from assistant.ui.icons import PALETTES, render  # noqa: E402

OUT = ROOT / "browser_extension" / "icons"
SIZES = (16, 32, 48, 128)


def main() -> None:
    if QGuiApplication.instance() is None:
        main.app = QGuiApplication(sys.argv)   # Qt paints only with an application object alive
    OUT.mkdir(parents=True, exist_ok=True)
    for key in PALETTES:
        for size in SIZES:
            render(key, size).save(str(OUT / f"{key}-{size}.png"))
    render("idle", 256).save(str(ROOT / "assistant" / "ui" / "jarvis.ico"))   # the program's icon (build)
    print(f"{len(PALETTES) * len(SIZES)} иконок в {OUT} и assistant/ui/jarvis.ico")


if __name__ == "__main__":
    main()
