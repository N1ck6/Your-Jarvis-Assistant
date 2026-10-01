"""Presentation: markdown on cards, picture window, windows side by side, voice vs screen split, weather questions,
several timers in one phrase, "Джарвис" in the hot window."""
import asyncio
import datetime as dt
import math
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from assistant import visuals  # noqa: E402
from assistant.assistant import split_notes  # noqa: E402
from assistant.core import Card, Deck  # noqa: E402
from assistant.nlu import normalize_command  # noqa: E402
from assistant.skills.timers import TimersSkill  # noqa: E402
from assistant.skills.weather import WeatherSkill  # noqa: E402
from assistant.ui.qt_ui import PANELS, DeckWindow, VisualWindow, inline_md, render_screen  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- markdown
@pytest.mark.parametrize("text,has,hasnt", [
    ("Это **важно**", "<b style='color:#FFFFFF'>важно</b>", "**"),
    ("**Итог:", "Итог:", "**"),                                   # an unpaired marker disappears
    ("код `x**2` тут", "x**2", "<b"),                              # code keeps its stars
    ("## Заголовок", "Заголовок", "## "),
    ("* пункт", "●", "*"),
    ("- **REST** — стиль", "<b style='color:#FFFFFF'>REST</b>", "**"),
    ("snake_case_name и _курсив_", "snake_case_name", "_курсив_"),
    ("| a | b |\n|---|---|\n| 1 | 2 |", "<b>a</b>", "---"),
    ("```\nprint(1)\n```", "print(1)", "```"),
])
def test_markdown_renders_without_raw_marks(text, has, hasnt):
    html = render_screen(text)
    assert has in html and hasnt not in html, html


def test_inline_md_escapes_html():
    assert "&lt;script&gt;" in inline_md("**<script>**")


# ---------------------------------------------------------------- windows
def test_single_card_hides_arrows(qapp):
    w = DeckWindow(25)
    w.set_deck(Deck("Ответ", [Card("", "- один пункт", "")], done=True))
    assert not w.prev_btn.isVisible() and not w.next_btn.isVisible()
    w.set_deck(Deck("Тема", [Card("1", "a", ""), Card("2", "b", "")], done=True))
    assert w.prev_btn.isVisible() and w.next_btn.isVisible()


def test_windows_sit_near_the_centre_side_by_side(qapp):
    vis, deck = VisualWindow(25), DeckWindow(25)
    PANELS.windows[:] = [vis, deck]
    deck.set_deck(Deck("Гипербола", [Card("", "- y = k/x", "")], done=True))
    vis.show_visual(visuals.Visual("y = 1/x", plot={"expr": "1/x", "label": "y = 1/x", "x": [-6, 6]}))
    screen = QApplication.primaryScreen().availableGeometry()
    a, b = vis.frameGeometry(), deck.frameGeometry()
    assert not a.intersects(b)
    if a.width() + b.width() + PANELS.GAP <= screen.width() - 24:
        assert b.left() - a.right() >= PANELS.GAP - 1
        assert abs((a.left() + b.right()) / 2 - screen.center().x()) < 40
        assert a.top() < screen.center().y()
    else:  # the offscreen test screen is 800 px wide: one under the other, centred
        assert b.top() - a.bottom() >= PANELS.GAP - 1
        assert abs(a.center().x() - screen.center().x()) < 40


def test_visual_window_draws_molecule_and_plot(qapp):
    w = VisualWindow(25)
    mol = {"atoms": [["C", 2, 0], ["C", 3, 0], ["H", 1.6, 0.6], ["H", 1.6, -0.6], ["H", 3.4, 0.6], ["H", 3.4, -0.6]],
           "bonds": [[0, 1, 2], [0, 2, 1], [0, 3, 1], [1, 4, 1], [1, 5, 1]], "explicit": True}
    w.show_visual(visuals.Visual("C₂H₄", "Двойная связь", molecule=mol, source="PubChem"))
    assert w.isVisible() and not w.picture.pixmap().isNull() and "PUBCHEM" not in w.title.text()
    w.show_visual(visuals.Visual("y = sin x", plot={"expr": "sin(x)", "label": "y = sin x", "x": [-6, 6]}))
    assert not w.picture.pixmap().isNull()


# ---------------------------------------------------------------- visuals
@pytest.mark.parametrize("phrase,kind", [
    ("что значит двойная связь", "molecule"), ("молекула кофеина", "molecule"), ("как выглядит гипербола", "plot"),
    ("покажи график y = x^2 - 3x", "plot"), ("как выглядит жираф", "photo"), ("построй график синуса", "plot"),
])
def test_visual_detect(phrase, kind):
    assert visuals.detect(phrase).kind == kind


@pytest.mark.parametrize("phrase", ["что такое REST API", "какая погода", "открой телеграм", "поставь таймер"])
def test_visual_not_for_plain_questions(phrase):
    assert visuals.detect(phrase) is None


def test_formula_is_arithmetic_only():
    assert visuals.parse_formula("y = 2x^2 - 3x + 1")(2) == 3.0
    assert visuals.parse_formula("|x|")(-3) == 3
    assert math.isnan(visuals.parse_formula("1/x")(0))
    for evil in ("__import__('os')", "x.real", "open('f')", "(lambda: 1)()", "[x for x in ()]"):
        assert visuals.parse_formula(evil) is None


def test_molecule_dictionary_and_concepts():
    assert visuals._molecule_name("молекула кофеина")[0] == "caffeine"
    name, caption = visuals._molecule_name("что значит двойная связь")
    assert name == "ethylene" and "C=C" in caption
    assert visuals._molecule_name("структурная формула метанола")[0] == "methanol"


def test_first_sentence_drops_brackets_and_stress():
    text = "Жира́ф (лат. Giraffa camelopardalis (Linnaeus)) — парнокопытное млекопитающее. Второе предложение."
    assert visuals.first_sentence(text) == "Жираф — парнокопытное млекопитающее."


# ---------------------------------------------------------------- voice vs screen
async def _split(parts):
    async def gen():
        for p in parts:
            yield p
    notes: list[str] = []
    spoken = "".join([d async for d in split_notes(gen(), notes)])
    return spoken, "".join(notes)


@pytest.mark.parametrize("parts,spoken,screen", [
    (["Две ветви прижимаются к осям.\n", "#", "##\n- y = k/x"], "Две ветви прижимаются к осям.\n", "- y = k/x"),
    (["Ответ без экрана, номер #1."], "Ответ без экрана, номер #1.", ""),
    (["Ответ.\nЭК", "РАН:\n- a"], "Ответ.\n", "- a"),
])
def test_screen_part_is_not_spoken(parts, spoken, screen):
    s, n = asyncio.run(_split(parts))
    assert s == spoken and n.strip() == screen


# ---------------------------------------------------------------- weather questions
@pytest.mark.parametrize("phrase,action", [
    ("когда будет следующий дождь", "ask"), ("когда пойдет снег", "ask"), ("когда закончится дождь", "ask"),
    ("в какой день на этой неделе будет теплее всего", "ask"), ("будет ли дождь в выходные", "ask"),
    ("какая погода", "forecast"), ("будет ли дождь вечером", "forecast"), ("погода в субботу", "forecast"),
])
def test_weather_question_kinds(phrase, action):
    assert WeatherSkill().match(normalize_command(phrase)).action == action


def _hourly(rain_from: int, rain_to: int, prob: int = 80):
    start = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
    times = [start + dt.timedelta(hours=i) for i in range(24 * 7)]
    wet = [rain_from <= i < rain_to for i in range(len(times))]
    return {"hourly": {"time": [t.isoformat(timespec="minutes") for t in times],
                       "weather_code": [63 if w else 2 for w in wet],
                       "precipitation_probability": [prob if w else 5 for w in wet],
                       "precipitation": [1.2 if w else 0 for w in wet]}}


def test_next_rain_in_the_week():
    text = WeatherSkill._next_precip(_hourly(30, 34), "дождь", "Москва")
    assert text.startswith("Дождь в Москве начнётся") and "около" in text and "4 часов" in text
    assert "не ожидается" in WeatherSkill._next_precip(_hourly(500, 500), "дождь", "Москва")
    assert "уже идёт" in WeatherSkill._next_precip(_hourly(0, 3), "дождь", "Москва")
    assert "не ожидается" in WeatherSkill._next_precip(_hourly(30, 34), "снег", "Москва")


def test_unlikely_drizzle_is_not_the_next_rain():
    text = WeatherSkill._next_precip(_hourly(30, 34, prob=20), "дождь", "Москва")
    assert "не ожидается" in text and "Небольшой шанс" in text


# ---------------------------------------------------------------- several timers
@pytest.mark.parametrize("phrase,items", [
    ("поставь таймер через минуту и другой через две", [[60, ""], [120, ""]]),
    ("таймеры на 5 и 10 минут", [[300, ""], [600, ""]]),
    ("напомни через 10 минут позвонить маме и через час выключить плиту", [[600, "позвонить маме"],
                                                                           [3600, "выключить плиту"]]),
])
def test_several_timers_in_one_phrase(phrase, items):
    intent = TimersSkill().match(normalize_command(phrase))
    assert intent.action == "set_many" and intent.slots["items"] == items


def test_hour_and_minutes_stay_one_timer():
    intent = TimersSkill().match(normalize_command("поставь таймер на 1 час и 20 минут"))
    assert intent.action == "set" and intent.slots == {"seconds": 4800, "label": ""}


# ---------------------------------------------------------------- formulas in LaTeX
@pytest.mark.parametrize("text,said", [
    ("Формула $y = x^2$ описывает параболу.", "игрек равно икс в квадрате"),
    (r"Площадь круга \(S = \pi r^2\).", "эс равно пи эр в квадрате"),
    (r"$$f'(x) = \lim_{h \to 0} \frac{f(x+h) - f(x)}{h}$$", "предел при аш стремящемся к нулю"),
    (r"Корни $x_{1,2} = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}$", "корень из бэ в квадрате минус четыре а цэ делить на два а"),
    ("Энергия $E = mc^2$.", "эм цэ в квадрате"),
])
def test_latex_is_spoken_as_words(text, said):
    from assistant.tts.text_norm import normalize_ru

    out = normalize_ru(text)
    assert said in out and "доллар" not in out and "$" not in out and "\\" not in out, out


def test_money_dollars_still_dollars():
    from assistant.tts.text_norm import normalize_ru

    assert normalize_ru("Это стоит $20 и $30.") == "Это стоит двадцать долларов и тридцать долларов."


@pytest.mark.parametrize("text,shown", [
    ("$y = x^2$", "y = x²"), (r"\(S = \pi r^2\)", "S = πr²"), ("$H_2O$", "H₂O"),
    (r"$x_{1,2} = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}$", "x₁,₂ = (-b ± √(b² - 4ac))/2a"),
])
def test_latex_is_shown_as_plain_formula(text, shown):
    from assistant.mathtext import to_display

    assert to_display(text) == shown
    assert "$" not in render_screen(text) and "\\" not in render_screen(text)
