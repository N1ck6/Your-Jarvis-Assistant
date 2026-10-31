"""A new commit restarts Jarvis by himself once he is idle; broken code does not."""
import asyncio

from assistant import selfupdate
from assistant.assistant import Assistant
from assistant.core import State


def test_head_reads_loose_and_packed_refs(tmp_path):
    assert selfupdate.head(tmp_path) is None
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "packed-refs").write_text("# pack-refs\naaa111 refs/heads/main\n")
    assert selfupdate.head(tmp_path) == "aaa111"
    (git / "refs" / "heads" / "main").write_text("bbb222\n")
    assert selfupdate.head(tmp_path) == "bbb222"


def test_the_code_of_this_checkout_passes_the_check(tmp_path):
    from assistant.paths import ROOT

    assert selfupdate.check(ROOT) == ""
    (tmp_path / "assistant").mkdir()
    (tmp_path / "assistant" / "broken.py").write_text("def f(:\n")
    assert selfupdate.check(tmp_path) != ""


def make_app(monkeypatch, heads, error=""):
    app = Assistant.__new__(Assistant)
    from assistant.config import load_settings

    app.cfg = load_settings()
    app.state = State.IDLE
    app._state_at = 0.0
    app._task = None
    app._dictating = False
    app.speaker = type("Speaker", (), {"speaking": False})()
    app.music = type("Music", (), {"state": "stopped"})()
    app.notes = []
    app.ui = type("Ui", (), {"notify": lambda self, title, text: app.notes.append(text)})()
    app.restarts = []

    async def handover(quiet, updated):
        app.restarts.append((quiet, updated))
        return True
    app._handover = handover
    seq = iter(heads)
    last = {"v": None}

    def head(root):
        last["v"] = next(seq, last["v"])
        return last["v"]
    monkeypatch.setattr(selfupdate, "head", head)
    monkeypatch.setattr(selfupdate, "check", lambda root: error)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
    return app


def test_a_new_commit_restarts_him_quietly_when_idle(monkeypatch):
    app = make_app(monkeypatch, ["a", "a", "b"])
    asyncio.run(asyncio.wait_for(app._watch_code(), 2))
    assert app.restarts == [(True, True)]


def test_broken_code_keeps_the_running_jarvis(monkeypatch):
    app = make_app(monkeypatch, ["a", "b"], error="SyntaxError: invalid syntax")

    async def run():
        task = asyncio.create_task(app._watch_code())
        for _ in range(2000):   # the check runs in a worker thread: wait for its verdict, not a fixed number of steps
            if app.notes:
                break
            await asyncio.sleep(0)
        task.cancel()
    asyncio.run(run())
    assert app.restarts == [] and len(app.notes) == 1 and "SyntaxError" in app.notes[0]


def test_busy_jarvis_is_not_idle():
    app = Assistant.__new__(Assistant)
    app.state, app._state_at, app._task, app._dictating = State.SPEAKING, 0.0, None, False
    app.speaker = type("Speaker", (), {"speaking": True})()
    app.music = type("Music", (), {"state": "stopped"})()
    assert app.idle_for() == 0.0
    app.state, app.speaker.speaking = State.IDLE, False
    app.music.state = "playing"
    assert app.idle_for() == 0.0
    app.music.state = "stopped"
    assert app.idle_for() > 0


class FakeProc:
    def __init__(self, exit_code=None):
        self.pid, self.exit_code, self.killed = 4242, exit_code, False

    def poll(self):
        return self.exit_code

    def kill(self):
        self.killed = True


def test_handover_waits_for_the_new_jarvis_then_exits(monkeypatch):
    app = make_app(monkeypatch, ["a"])
    del app._handover
    proc = FakeProc()
    ready = iter([False, False, True])
    monkeypatch.setattr(selfupdate, "spawn", lambda root, quiet, updated: proc)
    monkeypatch.setattr(selfupdate, "is_ready", lambda pid: next(ready, True))
    monkeypatch.setattr(selfupdate, "clear", lambda: None)
    exits = []
    app.on_exit = lambda: exits.append(True)
    assert asyncio.run(app._handover(True, True)) is True
    assert exits == [True]


def test_a_new_jarvis_that_dies_while_loading_leaves_this_one(monkeypatch):
    app = make_app(monkeypatch, ["a"])
    del app._handover
    monkeypatch.setattr(selfupdate, "spawn", lambda root, quiet, updated: FakeProc(exit_code=1))
    monkeypatch.setattr(selfupdate, "is_ready", lambda pid: False)
    monkeypatch.setattr(selfupdate, "clear", lambda: None)
    exits = []
    app.on_exit = lambda: exits.append(True)
    assert asyncio.run(app._handover(True, True)) is False
    assert exits == [] and app.notes


def test_ready_file_names_the_new_process(tmp_path, monkeypatch):
    monkeypatch.setattr(selfupdate, "READY", tmp_path / "handover.ready")
    import os

    monkeypatch.setenv("JARVIS_HANDOVER_FROM", str(os.getppid()))   # started directly, not through a launcher

    assert not selfupdate.is_ready(os.getpid())
    selfupdate.mark_ready()
    assert selfupdate.is_ready(os.getpid()) and not selfupdate.is_ready(os.getpid() + 1)
    selfupdate.clear()
    assert not selfupdate.is_ready(os.getpid())


def test_the_ready_mark_names_the_process_the_old_jarvis_started(monkeypatch):
    """Behind the venv's launcher the old Jarvis holds the launcher's pid (our parent); started directly, ours."""
    import os

    monkeypatch.setenv("JARVIS_HANDOVER_FROM", str(os.getppid()))
    assert selfupdate.spawned_pid() == os.getpid()
    monkeypatch.setenv("JARVIS_HANDOVER_FROM", "1")
    assert selfupdate.spawned_pid() == os.getppid()
