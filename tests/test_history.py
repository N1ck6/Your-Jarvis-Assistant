"""Request history: data/history.jsonl, "не то" marks a request wrong, the last minutes come back after a restart."""
import time

import pytest

from assistant import history


@pytest.fixture(autouse=True)
def file(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "FILE", tmp_path / "history.jsonl")


def test_records_are_kept_marked_and_cleared():
    rid = history.add("какой прокаст у пуджа", "Хук, потом ротация.", "answer: ответил …", 3.2)
    history.add("громче", "", "command: выполнено «громче»")
    assert [i["query"] for i in history.items()] == ["громче", "какой прокаст у пуджа"]
    assert history.mark(rid, True, "нужно было из интернета")
    assert history.items()[1]["wrong"] and history.items()[1]["note"] == "нужно было из интернета"
    history.mark_last_wrong()
    assert history.items()[0]["wrong"]
    history.delete(rid)
    assert len(history.items()) == 1
    history.clear()
    assert history.items() == []


def test_only_the_last_minutes_come_back():
    history.add("старый", "")
    items = history._read()
    items[0]["id"] = f"{time.time() - 3600:.3f}"
    history._write(items)
    history.add("свежий", "ответ")
    assert [i["query"] for i in history.recent(600)] == ["свежий"]


def test_spoken_math_is_shown_with_symbols_in_the_tray():
    from assistant.mathtext import spoken_to_display as show

    assert show("построй мне график уравнения 3 синус 2c квадрат") == "построй мне график уравнения 3·sin(2c²)"
    assert show("график корень из икс плюс 2 икс в кубе") == "график √x + 2x³"
    assert show("график y равно 2 в степени икс") == "график y = 2ˣ"
    assert show("график икс в степени минус 2") == "график x⁻²"
    assert show("нарисуй квадрат") == "нарисуй квадрат"
    assert show("какая погода завтра") == "какая погода завтра"
