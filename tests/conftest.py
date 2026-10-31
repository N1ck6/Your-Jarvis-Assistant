"""Shared for all tests: the owner's data/history.jsonl is never written (his settings may keep the history on)."""
import pytest


@pytest.fixture(autouse=True)
def _own_history_file(tmp_path, monkeypatch):
    from assistant import history

    monkeypatch.setattr(history, "FILE", tmp_path / "history.jsonl")
