"""Window defects found on real answers: code split into a box per line, a request as the window header, a graph
missing without the local model, ASCII drawings next to a real picture, a short card in a tall window."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from assistant import visuals  # noqa: E402
from assistant.assistant import card_title  # noqa: E402
from assistant.core import Card, Deck  # noqa: E402
from assistant.ui.qt_ui import PANELS, DeckWindow, VisualWindow, _caps, card_plain, render_screen  # noqa: E402

BUBBLE = """- **n** — длина списка
> python
> def bubble_sort(arr):
>     n = len(arr)
>     for i in range(n):
>         pass
>     return arr"""

DRAWING = """- Формула: C₈H₁₀N₄O₂
- Структурная формула:
> O
> ||
> N-C-N
> | \\
> C C=O
> | /
- Содержится в кофе"""


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_code_lines_are_one_box_with_indentation():
    html = render_screen(BUBBLE)
    assert html.count("background-color:#0F131B") == 1
    assert "    n = len(arr)" in html and "pre-wrap" in html
    assert ">python<" not in html and "python<br>" not in html


def test_ascii_drawing_is_dropped_with_its_label():
    html = render_screen(DRAWING)
    assert "||" not in html and "N-C-N" not in html and "Структурная формула" not in html
    assert "Содержится в кофе" in html and "C₈H₁₀N₄O₂" in html


def test_copy_gives_clean_code():
    text = card_plain(BUBBLE)
    assert "def bubble_sort(arr):\n    n = len(arr)" in text and "> " not in text and "python\n" not in text
    assert "||" not in card_plain(DRAWING)


@pytest.mark.parametrize("query,title", [
    ("покажи мне график x квадрат- 3 x", "График y = x² − 3x"),
    ("расскажи мне про чёрные дыры", "Чёрные дыры"),
    ("а скажи, сколько стоит RTX 4060?", "Сколько стоит RTX 4060"),
    ("Как реализовать пузырьковую сортировку на питоне", "Как реализовать пузырьковую сортировку на питоне"),
])
def test_card_title_is_the_subject(query, title):
    assert card_title(query) == title


def test_formula_titles_keep_their_case():
    assert _caps("График y = x² − 3x") == "График y = x² − 3x"
    assert _caps("Молекула кофеина") == "МОЛЕКУЛА КОФЕИНА"


@pytest.mark.parametrize("phrase,expr,x,y", [
    ("покажи мне график x квадрат- 3 x", "^2", 3, 0),
    ("график игрек равно синус икс", "sin", 0, 0),
    ("построй график функции икс в кубе минус два икс", "^3", 2, 4),
    ("график единица делить на икс", "/", 2, 0.5),
])
def test_spoken_formula_without_a_model(phrase, expr, x, y):
    formula = visuals.spoken_formula(phrase)
    assert expr in formula and abs(visuals.parse_formula(formula)(x) - y) < 1e-9


def test_graph_of_weather_is_not_a_function_graph():
    from assistant.skills.visual import VisualSkill

    assert VisualSkill().match("график погоды") is None
    assert VisualSkill().match("покажи мне график x квадрат минус 3 x") is not None


def test_short_card_shrinks_the_window(qapp):
    w = DeckWindow(25)
    w.set_deck(Deck("a", [Card("", "\n".join(f"- пункт {i}" for i in range(14)), "")], done=True))
    qapp.processEvents()
    tall = w.height()
    w.set_deck(Deck("b", [Card("", "- один\n- два", "")], done=True))
    qapp.processEvents()
    assert w.height() < tall - 150


def test_windows_line_up_beside_a_dragged_one(qapp):
    vis, deck = VisualWindow(25), DeckWindow(25)
    PANELS.windows[:] = [vis, deck]
    deck.set_deck(Deck("Тема", [Card("", "- пункт", "")], done=True))
    deck._user_pos = QPoint(259, 0)   # the offscreen screen is 800 px: no room at the sides, so below
    deck.move(259, 0)
    vis.show_visual(visuals.Visual("y = 1/x", plot={"expr": "1/x", "label": "y = 1/x", "x": [-6, 6]}))
    qapp.processEvents()
    assert not vis.frameGeometry().intersects(deck.frameGeometry())


def test_answer_cards_stay_long_enough_to_read():
    from assistant.core import reading_time

    assert reading_time("Коротко.") == 45
    assert 60 <= reading_time(" ".join(["слово"] * 80)) <= 80
    assert reading_time(" ".join(["слово"] * 1000)) == 150


def test_pinned_card_has_no_timer_and_its_own_window(qapp):
    from assistant.ui.qt_ui import QtUi

    ui = QtUi(25)
    ui._route_deck(Deck("Ответ", [Card("", "- обычный", "")], done=True, linger=50))
    assert ui.window._close_timer.isActive() and ui.window._close_timer.interval() == 50000
    ui._route_deck(Deck("Перевод", [Card("", "- важное", "")], done=True, pinned=True))
    pinned = ui.pinned[0]
    assert pinned is not ui.window and not pinned._close_timer.isActive() and pinned.pin_mark.isVisible()
    ui._route_deck(Deck("Следующий ответ", [Card("", "- новое", "")], done=True))
    assert pinned.deck.title == "Перевод" and pinned.isVisible()        # the next answer did not replace it
    pinned.on_user_close()
    QApplication.processEvents()
