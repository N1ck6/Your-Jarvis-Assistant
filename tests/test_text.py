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
    assert "пайтон" in out and "эй пи ай" in out
    assert "*" not in out and "+" not in out


@pytest.mark.parametrize("text,expected", [
    # units agree with the number
    ("Сейчас +24 °C, ветер Ю-З 2 м/с.", "плюс двадцать четыре градуса, ветер юго-западный два метра в секунду"),
    ("Влажность 81%, давление 745 мм рт. ст.", "восемьдесят один процент, давление семьсот сорок пять миллиметров ртутного столба"),
    ("Свободно 188 ГБ из 952,8 ГБ", "сто восемьдесят восемь гигабайт из девятьсот пятьдесят две целых восемь десятых гигабайта"),
    ("через 21 минуту и 2 секунды", "через двадцать одну минуту и две секунды"),
    ("13 млн человек", "тринадцать миллионов человек"),
    # money
    ("Курс доллара — 84,43 ₽ за 1 USD.", "восемьдесят четыре рубля сорок три копейки за один доллар"),
    ("Бюджет $1,5 млрд", "полтора миллиарда долларов"),
    ("Цена 1 999 ₽", "одна тысяча девятьсот девяносто девять рублей"),
    ("Курс USD вырос", "Курс доллара вырос"),
    ("EUR/RUB", "евро к рублю"),
    # dates and ordinals
    ("Сегодня среда, 1 октября.", "Сегодня среда, первое октября."),
    ("до 5 мая", "до пятого мая"),
    ("в 2024 году", "в две тысячи двадцать четвёртом году"),
    ("в XXI веке", "в двадцать первом веке"),
    ("в 90-х", "в девяностых"),
    # English for a Russian voice
    ("Видеокарта RTX 3060 Ti", "эр тэ икс тридцать шестьдесят ти ай"),
    ("RTX 4090, процессор i7-13700K", "эр тэ икс сорок девяносто, процессор ай семь тринадцать семьсот кей"),
    ("Объясни концепт REST API.", "рест эй пи ай"),
    ("Docker, Kubernetes и C++ или C#", "докер, кубернетес и си плюс плюс или си шарп"),
    ("Python 3.12", "пайтон три точка двенадцать"),
    ("подключи USB", "ю эс би"),
    ("Открой GitHub", "гитхаб"),
    ("4K и 5G", "четыре кей и пять джи"),
    ("сайт habr.com", "хабр точка ком"),
])
def test_normalize_for_speech(text, expected):
    assert expected in normalize_ru(text)


def test_english_words_from_phonemes():
    from assistant.tts import english

    assert english.from_phonemes("K AE1 N".split()) == "кэн"
    assert english.from_phonemes("K AH0 M P Y UW1 T ER0".split()) == "кампь+ютер"
    assert english.by_rules("make") == "мак"
    assert english.spell("SQL") == "эс кью эл"
    assert english.model_number("6600") == "шесть тысяч шестьсот"



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
