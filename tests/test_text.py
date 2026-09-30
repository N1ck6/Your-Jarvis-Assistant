import pytest

from assistant.brain import is_info_question
from assistant.nlu import find_wake, in_place, parse_duration, say_duration, strip_wake, to_nominative, words_to_numbers
from assistant.skills.explain import parse_deck
from assistant.speech import SentenceSplitter
from assistant.tts.text_norm import normalize_ru

WAKE = ["джарвис", "джервис"]


@pytest.mark.parametrize("text,expected", [
    ("через двадцать пять минут", "через 25 минут"),
    ("на сто двадцать секунд", "на 120 секунд"),
    ("пять минут", "5 минут"),
    ("один час тридцать минут", "1 час 30 минут"),
])
def test_words_to_numbers(text, expected):
    assert words_to_numbers(text) == expected


@pytest.mark.parametrize("text,seconds", [
    ("поставь таймер на 5 минут", 300),
    ("напомни через полчаса", 1800),
    ("через полтора часа", 5400),
    ("через 1 час 20 минут", 4800),
    ("таймер на тридцать секунд", 30),
    ("через пару минут", 120),
    ("напомни через пятнадцать минут выключить плиту", 900),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text)[0] == seconds


def test_parse_duration_none():
    assert parse_duration("напомни купить хлеб") is None


def test_say_duration():
    assert say_duration(90) == "1 минуту 30 секунд"
    assert say_duration(3600 + 120) == "1 час 2 минуты"


@pytest.mark.parametrize("text,query", [
    ("Джарвис, какая погода?", "какая погода?"),
    ("Эй, Джарвис, открой телеграм", "открой телеграм"),
    ("Какая погода, Джарвис?", "Какая погода ?"),
    ("Жарвис, который час", "который час"),
    ("Джарвис.", ""),
])
def test_strip_wake(text, query):
    assert strip_wake(text, WAKE).replace(" ?", "?") == query.replace(" ?", "?")


def test_find_wake_rejects_other_words():
    assert find_wake("давай посмотрим", WAKE) is None
    assert find_wake("джервис, привет", WAKE) is not None


@pytest.mark.parametrize("text,expected", [
    ("Казани", "Казань"), ("Нижнем Новгороде", "Нижний Новгород"), ("Москве", "Москва"),
])
def test_to_nominative(text, expected):
    assert to_nominative(text) == expected


def test_in_place():
    assert in_place("Москва") == "в Москве"
    assert in_place("Нижний Новгород") == "в Нижнем Новгороде"


def test_normalize_numbers_and_symbols():
    out = normalize_ru("**Сейчас** +12°C, в 14:30 будет -2. Python и API")
    assert "плюс двенадцать градусов" in out
    assert "четырнадцать тридцать" in out
    assert "минус два" in out
    assert "пайтон" in out and "апи" in out
    assert "*" not in out


def test_sentence_splitter_streams():
    s = SentenceSplitter()
    out = []
    for delta in ["Привет", "! Сейчас 3.5 гра", "дуса. Завтра", " теплее"]:
        out += s.feed(delta)
    out += s.flush()
    assert out == ["Привет!", "Сейчас 3.5 градуса.", "Завтра теплее"]


def test_sentence_splitter_soft_break_first():
    s = SentenceSplitter(first_soft_limit=40)
    out = s.feed("Это довольно длинное первое предложение, которое не заканчивается ")
    assert out and out[0].endswith(",")


@pytest.mark.parametrize("text,info", [
    ("что такое квантовый компьютер", True),
    ("какой курс доллара", True),
    ("кто выиграл вчерашний матч", True),
    ("как дела", False),
    ("расскажи шутку", False),
    ("мне скучно", False),
])
def test_info_routing(text, info):
    assert is_info_question(text) is info


def test_parse_deck_streaming():
    text = ("ЗАГОЛОВОК: DNS простыми словами\n###\nКАРТОЧКА: Суть\nЭКРАН:\n- Имя → адрес\n- Телефонная книга\n"
            "ГОЛОС: Это как записная книжка интернета.\n###\nКАРТОЧКА: Как рабо")
    partial = parse_deck(text, final=False)
    assert partial.title == "DNS простыми словами"
    assert len(partial.cards) == 1
    assert partial.cards[0].screen.startswith("- Имя")
    final = parse_deck(text + "тает\nЭКРАН:\n- Запрос\nГОЛОС: Компьютер спрашивает сервер.", final=True)
    assert len(final.cards) == 2


def test_parse_deck_tolerates_case_and_spaces():
    text = "ЗАГОЛОВОК: ОЗУ  \n###  \nКАРТОЧКА: Суть  \nЭКРАН:  \n- Быстро  \nГОЛос: Как рабочий стол.  \n"
    deck = parse_deck(text, final=True)
    assert deck.title == "ОЗУ"
    assert len(deck.cards) == 1 and deck.cards[0].speech == "Как рабочий стол."


def test_parse_deck_title_waits_for_line_end():
    assert parse_deck("ЗАГОЛОВОК: Что", final=False).title == ""


@pytest.mark.parametrize("text,expected", [
    ("Джарвис, будь добр, открой «Телеграм»!", "джарвис будь добр открой телеграм"),
    ("Будь добр, открой телеграм, пожалуйста.", "открой телеграм"),
    ("Можешь сделать погромче?", "сделать погромче"),
    ("Срочно выключи звук!", "выключи звук"),
    ("Зайди на habr.com", "зайди на habr.com"),
    ("Давай", "давай"),
    ("Ещё раз", "еще раз"),
])
def test_normalize_command(text, expected):
    from assistant.nlu import normalize_command

    assert normalize_command(text) == expected


def test_split_long_dictation():
    import numpy as np

    from assistant.stt import split_at_pauses

    sr = 16000
    speech = np.random.default_rng(0).normal(0, 0.3, sr * 12).astype(np.float32)
    pause = np.zeros(sr // 2, dtype=np.float32)
    audio = np.concatenate([speech, pause, speech, pause, speech])  # ~37 s
    chunks = split_at_pauses(audio, sr, max_sec=20)
    assert len(chunks) >= 2 and all(len(c) <= 20 * sr for c in chunks)
    assert sum(len(c) for c in chunks) == len(audio)
    first_cut = len(chunks[0])
    assert 12 * sr <= first_cut <= 12.5 * sr  # cut inside the pause, not mid-speech


def test_voice_packs_reactions():
    from assistant.voicepack import PACKS_DIR, VoicePack

    pack = VoicePack.load("jarvis-remaster")
    if pack is None:
        pytest.skip(f"no packs in {PACKS_DIR}")
    for reaction in ("reply", "ok", "thanks", "joke", "stupid", "greet_morning"):
        assert pack.has(reaction), reaction
    first = pack.pick("ok")
    second = pack.pick("ok")
    assert first.sr > 0 and first.samples.size > 1000
    assert first is not second  # never the same clip twice in a row
    assert pack.greeting() is not None
