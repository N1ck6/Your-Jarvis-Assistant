"""Spoken names against real ones: English song titles said in Russian letters, misheard sounds, and a question
instead of a guess when two titles are equally close."""
import asyncio
from pathlib import Path

from assistant import fuzzy
from assistant.skills.scenarios import main as scenarios
from tests.test_skills import app  # noqa: F401  (the fixture with every real skill)

TITLES = ["Trouble", "Black Magic", "Thrift Shop", "Panda Eyes", "Damage", "Temple", "Kai Theme", "I Wonder How It'll End",
          "Escape From Heaven", "Marble Machine", "Market madness", "Bad Apple!!", "Bad Boy", "Nautilus",
          "Megalo Strike Back Remix", "Megalovania Remix", "Кукла колдуна"]


def test_english_titles_said_in_russian_are_found():
    for said, title in [("трабл", "Trouble"), ("блэк мэджик", "Black Magic"), ("трифт шоп", "Thrift Shop"),
                        ("панда айз", "Panda Eyes"), ("дэмэдж", "Damage"), ("темпл", "Temple"),
                        ("ай вандер хау итл энд", "I Wonder How It'll End"), ("марбл машин", "Marble Machine"),
                        ("бэд эппл", "Bad Apple!!"), ("мегало страйк бэк", "Megalo Strike Back Remix"),
                        ("кукла колдуна", "Кукла колдуна"), ("Black magic", "Black Magic")]:
        assert fuzzy.pick(said, TITLES).best == title, said


def test_other_words_are_not_songs():
    for said in ["телеграм", "калькулятор", "дота", "что-нибудь бодрое"]:
        assert fuzzy.pick(said, TITLES).best is None, said


def test_close_titles_are_asked_about():
    found = fuzzy.pick("бэд", TITLES)
    assert found.best is None and {"Bad Apple!!", "Bad Boy"} <= set(found.close)


def test_the_answer_to_which_one():
    options = ["Bad Apple!!", "Bad Boy"]
    assert fuzzy.choose("вторую", options) == "Bad Boy"
    assert fuzzy.choose("бэд бой", options) == "Bad Boy"
    assert fuzzy.choose("да", options) == "Bad Apple!!"
    assert fuzzy.choose("включи погоду", options) is None


class FakePlayer:
    active, current, queue, index = False, None, [], 0

    def __init__(self):
        self.played = []

    def play(self, order, index=0):
        self.played.append(Path(order[index]).stem)
        return True

    def stop(self):
        pass


def test_music_asks_instead_of_playing_the_wrong_song(app, monkeypatch):  # noqa: F811
    music = next(s for s in app.skills if s.name == "music")
    monkeypatch.setattr(music, "tracks", lambda *_: {t.lower(): f"C:/music/{t}.mp3" for t in TITLES})
    monkeypatch.setattr(app, "music", FakePlayer(), raising=False)
    from assistant.skills.base import Intent

    reply = asyncio.run(music.handle(Intent("music", "play", {"name": "бэд"}, "включи бэд")))
    assert app.music.played == [] and reply.ask is not None and "Bad Apple!!" in reply.speech
    second = reply.speech.split("«")[2].split("»")[0]   # "Включить «A» или «B»?"
    reply = asyncio.run(reply.ask("второй"))
    assert app.music.played == [second]
    reply = asyncio.run(music.handle(Intent("music", "play", {"name": "трабл"}, "включи трабл")))
    assert app.music.played[-1] == "Trouble"
    assert music.match("включи трабл").action == "play" and music.match("включи телеграм") is None


def test_scenarios_file_round_trip_and_checks():
    items = [{"name": "Утро", "phrases": ["я проснулся", " "], "say": "", "steps": [
        {"command": "доброе утро"}, {"open": ""}, {"timer": "5,5", "label": "чай"}, {"minimize_all": "да"}, {"music": ""}]}]
    clean = scenarios.check(items)
    assert clean[0]["phrases"] == ["я проснулся"]
    assert clean[0]["steps"] == [{"command": "доброе утро"}, {"timer": 5.5, "label": "чай"}, {"minimize_all": True},
                                 {"music": ""}]
    assert scenarios.load(scenarios.dump(clean)) == clean
    long = [{"name": "Фокус", "phrases": [f"фраза номер {i}" for i in range(12)], "say": "", "steps": []}]
    text = scenarios.dump(scenarios.check(long))
    assert max(len(line) for line in text.splitlines() if not line.startswith("#")) <= 110
    assert scenarios.load(text)[0]["phrases"] == long[0]["phrases"]
    for bad in ([{"name": "A", "phrases": [], "steps": []}],
                [{"name": "A", "phrases": ["фокус"]}, {"name": "B", "phrases": ["Фокус"]}],
                [{"name": "A", "phrases": ["x"], "steps": [{"wait": "долго"}]}]):
        try:
            scenarios.check(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
