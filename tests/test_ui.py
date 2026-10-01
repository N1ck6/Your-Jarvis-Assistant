"""Qt smoke tests (offscreen): card window, paging and closing callbacks, card text rendering, hotkey parsing."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from assistant.core import Card, Deck, State  # noqa: E402
from assistant.hotkeys import parse  # noqa: E402
from assistant.ui.qt_ui import DeckWindow, render_screen, state_icon  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def deck(n=3, done=True):
    return Deck("Тема", [Card(f"Карточка {i + 1}", f"- пункт {i + 1}\n1. шаг\n> код {i}", f"голос {i}") for i in range(n)],
                done=done)


def test_deck_window_pages_and_reports_manual_paging(qapp):
    w = DeckWindow(25)
    paged, closed = [], []
    w.on_user_nav, w.on_user_close = paged.append, lambda: closed.append(1)
    w.set_deck(deck())
    assert w.isVisible() and w.index == 0
    QTest.keyClick(w, Qt.Key.Key_Right)
    QTest.keyClick(w, Qt.Key.Key_Right)
    QTest.keyClick(w, Qt.Key.Key_Right)          # already on the last card: nothing to report
    QTest.keyClick(w, Qt.Key.Key_Left)
    assert paged == [1, 2, 1] and w.index == 1
    w.show_card(2)                                # the narration moves on: not a manual page
    assert paged == [1, 2, 1]
    QTest.keyClick(w, Qt.Key.Key_Escape)
    assert closed == [1]


def test_deck_window_keeps_the_dragged_position(qapp):
    w = DeckWindow(25)
    w.set_deck(deck())
    w._user_pos = QPoint(100, 120)
    w.show_card(1)
    w.set_deck(deck(2))
    assert (w.pos().x(), w.pos().y()) == (100, 120)


def test_long_card_scrolls_instead_of_growing_off_screen(qapp):
    w = DeckWindow(25)
    long_text = "\n".join(f"- пункт номер {i} с достаточно длинным пояснением, чтобы перенестись" for i in range(80))
    w.set_deck(Deck("Длинно", [Card("Много", long_text, "")], done=True))
    screen = QApplication.primaryScreen().availableGeometry()
    assert w.height() <= screen.height()


def test_copy_button_copies_the_card(qapp):
    w = DeckWindow(25)
    w.set_deck(deck())
    w._copy()
    assert "Карточка 1" in QApplication.clipboard().text() and "пункт 1" in QApplication.clipboard().text()


def test_render_screen_kinds():
    html = render_screen("- REST — стиль API\n2. Сервер отвечает\n> GET /users/42\nИтог в одну строку")
    assert "<b style='color:#FFFFFF'>REST</b>" in html
    assert ">2</td>" in html and "GET /users/42" in html and "monospace" in html and "Итог в одну строку" in html
    assert "&lt;" in render_screen("- a < b")


def test_state_icons(qapp):
    for state in (None, *State):
        assert not state_icon(state).isNull()


@pytest.mark.parametrize("combo,mods,vk", [
    ("<ctrl>+<alt>+j", 0x0003, ord("J")), ("<ctrl>+<alt>+k", 0x0003, ord("K")), ("<ctrl>+<shift>+<f5>", 0x0006, 0x74),
])
def test_hotkey_parse(combo, mods, vk):
    assert parse(combo) == (mods, vk)


def test_hotkey_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse("<ctrl>+<alt>")
    with pytest.raises(ValueError):
        parse("<ctrl>+ж")  # a punctuation key in the Russian layout
    assert parse("<ctrl>+<alt>+о") == (0x0003, ord("J"))  # Russian layout letter = the same key


def test_chat_window_history_send_copy(qapp):
    from assistant.ui.qt_ui import ChatWindow

    w = ChatWindow()
    sent = []
    w.on_send = sent.append
    w.add("user", "Какая погода?")
    w.add("assistant", "Плюс двенадцать, облачно.")
    w.input.setText("  открой телеграм ")
    w._send()
    assert sent == ["открой телеграм"] and w.input.text() == ""
    w._copy_last()
    assert QApplication.clipboard().text() == "Плюс двенадцать, облачно."
    assert "Плюс двенадцать" in w.view.toPlainText()


def test_hud_click_listens_and_drag_does_not(qapp):
    from assistant.ui.qt_ui import HudWindow

    h = HudWindow()
    clicks = []
    h.on_click = lambda: clicks.append(1)
    h.set_state(State.THINKING)
    h._tick()
    QTest.mouseClick(h, Qt.MouseButton.LeftButton)
    assert clicks == [1]
    assert not h.grab().isNull()
