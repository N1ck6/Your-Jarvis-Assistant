"""Pictures next to an explanation, where seeing beats hearing: a function graph, a molecule, a photo.

detect() is a cheap regex check (no network, microseconds) run on questions and explanation topics;
fetch() then builds the picture: graphs are drawn locally from a safely parsed formula, molecules come from
PubChem (structure image by name, free, no key), everything else from the Wikipedia article image.
When a dictionary has no answer, the local model turns the phrase into a formula or an English compound name.
"""
from __future__ import annotations

import ast
import logging
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable
from urllib.parse import quote

import httpx

from assistant.llm.providers import Msg
from assistant.log import private
from assistant.nlu import normalize_command

if TYPE_CHECKING:
    from assistant.assistant import Assistant

log = logging.getLogger("visuals")
UA = {"User-Agent": "JarvisDesktopAssistant/1.0 (https://github.com/N1ck6/Your-Jarvis-Assistant)"}


@dataclass
class Visual:
    title: str
    caption: str = ""
    image: bytes = b""                     # PNG / JPEG
    light: bool = False                    # the image has a white background (structure drawings)
    plot: dict = field(default_factory=dict)  # {"expr": "1/x", "label": "y = 1/x", "x": [-6, 6]}
    # Structural formula drawn by the UI: {"atoms": [["C", x, y], ...], "bonds": [[i, j, order], ...]}
    molecule: dict = field(default_factory=dict)
    source: str = ""


@dataclass
class Request:
    kind: str          # plot | molecule | photo
    subject: str       # what was asked, as said


# ---------------------------------------------------------------- formulas (graphs)
CURVES: dict[str, tuple[str, str, tuple[float, float]]] = {
    # word stem -> (expression in x, label, x range)
    "гипербол": ("1/x", "y = 1/x", (-6, 6)),
    "кубическ": ("x**3", "y = x³", (-2.2, 2.2)),
    "парабол": ("x**2", "y = x²", (-4, 4)),
    "синусоид": ("sin(x)", "y = sin x", (-2 * math.pi, 2 * math.pi)),
    "синус": ("sin(x)", "y = sin x", (-2 * math.pi, 2 * math.pi)),
    "косинус": ("cos(x)", "y = cos x", (-2 * math.pi, 2 * math.pi)),
    "котангенс": ("1/tan(x)", "y = ctg x", (-2 * math.pi, 2 * math.pi)),
    "тангенс": ("tan(x)", "y = tg x", (-2 * math.pi, 2 * math.pi)),
    "экспонент": ("exp(x)", "y = eˣ", (-3, 2.5)),
    "логарифм": ("ln(x)", "y = ln x", (0.02, 8)),
    "квадратн корн": ("sqrt(x)", "y = √x", (0, 9)),
    "корн": ("sqrt(x)", "y = √x", (0, 9)),
    "модул": ("abs(x)", "y = |x|", (-5, 5)),
    "прям": ("x", "y = x", (-5, 5)),
    "линейн": ("2*x+1", "y = 2x + 1", (-4, 4)),
    "гаусс": ("exp(-x**2/2)", "y = e^(−x²/2)", (-4, 4)),
    "нормальн распредел": ("exp(-x**2/2)", "y = e^(−x²/2)", (-4, 4)),
    "сигмоид": ("1/(1+exp(-x))", "y = 1 / (1 + e⁻ˣ)", (-8, 8)),
}
_FUNCS: dict[str, Callable] = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "tg": math.tan, "ctg": lambda v: 1 / math.tan(v),
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "arcsin": math.asin, "arccos": math.acos,
    "arctg": math.atan, "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh, "sqrt": math.sqrt,
    "exp": math.exp, "ln": math.log, "log": math.log, "lg": math.log10, "log10": math.log10, "log2": math.log2,
    "abs": abs, "floor": math.floor, "ceil": math.ceil,
}
_CONSTS = {"pi": math.pi, "e": math.e}
_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.USub, ast.UAdd, ast.Mod)


def parse_formula(text: str) -> Callable[[float], float] | None:
    """'y = 2x^2 - 3x + 1' -> f(x). Only numbers, x, + - * / ^, and math functions: nothing else gets evaluated."""
    expr = text.strip().lower().replace("−", "-").replace("·", "*").replace("×", "*").replace("^", "**").replace(",", ".")
    expr = re.sub(r"√\s*(\([^)]*\)|[\w.]+)", r"sqrt(\1)", expr.replace("²", "**2").replace("³", "**3"))
    expr = re.sub(r"^\s*(?:y|f\s*\(\s*x\s*\))\s*=\s*", "", expr)
    expr = re.sub(r"(\d)\s*(x|\()", r"\1*\2", expr)           # 2x -> 2*x, 3(x+1) -> 3*(x+1)
    expr = re.sub(r"\)\s*(x|\(|\d)", r")*\1", expr)            # (x+1)(x-1) -> (x+1)*(x-1)
    expr = re.sub(r"\bx\s*(\(|x)", r"x*\1", expr)              # x(x+1), xx
    expr = re.sub(r"\|([^|]+)\|", r"abs(\1)", expr)            # |x| -> abs(x)
    if not expr or len(expr) > 120:
        return None
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords):
                return None
        elif isinstance(node, ast.Name):
            if node.id not in _FUNCS and node.id not in _CONSTS and node.id != "x":
                return None
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)):
                return None
        elif not isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Load, *_OPS)):
            return None
    code = compile(tree, "<formula>", "eval")
    env = {"__builtins__": {}, **_FUNCS, **_CONSTS}

    def f(x: float) -> float:
        try:
            y = eval(code, env, {"x": x})  # noqa: S307 - the tree above holds only arithmetic on x
            return float(y) if not isinstance(y, complex) and abs(y) < 1e12 else math.nan
        except (ArithmeticError, ValueError, TypeError):
            return math.nan
    return f


# ---------------------------------------------------------------- molecules
MOLECULES = {
    "кофеин": "caffeine", "вода": "water", "этанол": "ethanol", "спирт": "ethanol", "этиловый спирт": "ethanol",
    "метанол": "methanol", "глюкоза": "glucose", "фруктоза": "fructose", "сахароза": "sucrose", "сахар": "sucrose",
    "бензол": "benzene", "метан": "methane", "этан": "ethane", "пропан": "propane", "бутан": "butane",
    "этилен": "ethylene", "ацетилен": "acetylene", "аспирин": "aspirin", "парацетамол": "paracetamol",
    "ибупрофен": "ibuprofen", "никотин": "nicotine", "серотонин": "serotonin", "дофамин": "dopamine",
    "адреналин": "epinephrine", "мелатонин": "melatonin", "тестостерон": "testosterone", "холестерин": "cholesterol",
    "углекислый газ": "carbon dioxide", "аммиак": "ammonia", "ацетон": "acetone", "уксусная кислота": "acetic acid",
    "серная кислота": "sulfuric acid", "азотная кислота": "nitric acid", "соляная кислота": "hydrochloric acid",
    "лимонная кислота": "citric acid", "аскорбиновая кислота": "ascorbic acid", "витамин c": "ascorbic acid",
    "озон": "ozone", "кислород": "oxygen", "азот": "nitrogen", "водород": "hydrogen", "хлорофилл": "chlorophyll a",
    "пенициллин": "penicillin G", "морфин": "morphine", "капсаицин": "capsaicin", "ментол": "menthol",
    "глицерин": "glycerol", "мочевина": "urea", "формальдегид": "formaldehyde", "толуол": "toluene",
    "фенол": "phenol", "нафталин": "naphthalene", "адф": "adenosine diphosphate", "атф": "adenosine triphosphate",
    "глицин": "glycine", "аланин": "alanine", "триптофан": "tryptophan",
}
# Chemistry concepts shown on a typical molecule.
CONCEPTS = {
    "двойн": ("ethylene", "Двойная связь C=C в молекуле этилена: σ-связь и π-связь между атомами углерода."),
    "тройн": ("acetylene", "Тройная связь C≡C в молекуле ацетилена: одна σ-связь и две π-связи."),
    "одинарн": ("ethane", "Одинарная связь C–C в молекуле этана."),
    "ароматич": ("benzene", "Бензольное кольцо: шесть атомов углерода, π-электроны общие для всего кольца."),
    "бензольн": ("benzene", "Бензольное кольцо: шесть атомов углерода, π-электроны общие для всего кольца."),
    "пептидн": ("glycylglycine", "Пептидная связь –CO–NH– между двумя аминокислотами (глицилглицин)."),
    "эфирн": ("ethyl acetate", "Сложноэфирная группа –COO– в этилацетате."),
    "водородн": ("water", "Молекула воды: водородные связи возникают между O и H соседних молекул."),
    "ковалентн": ("methane", "Ковалентные связи C–H в молекуле метана: общие электронные пары."),
}

_PLOT = re.compile(r"\b(график\w*|гипербол\w*|парабол\w*|синусоид\w*|косинусоид\w*|экспонент\w*|сигмоид\w*|"
                   r"функци\w* (?:y|игрек|корня|модуля|синус\w*|косинус\w*|тангенс\w*|логарифм\w*|экспонент\w*)|"
                   r"построй \w+|нарисуй (?:график|функци\w*|параболу|гиперболу))")
_FORMULA = re.compile(r"\b(?:y|игрек|f от x)\s*(?:=|равно|равен)\s*([0-9a-z+\-*/^().,|²³√ ]+)")
_MOLECULE = re.compile(r"\b(молекул\w*|структурн\w* формул\w*|строени\w+ (?:молекул\w*|вещества)|"
                       r"(?:двойн|тройн|одинарн|ковалентн|водородн|пептидн|сложноэфирн|эфирн)\w* связ\w*|"
                       r"бензольн\w* кольц\w*|ароматическ\w* кольц\w*|химическ\w* формул\w*)")
_LOOK = re.compile(r"^(?:а\s+)?(?:как выгляд(?:ит|ят|ел|ела|ело|ели)|покажи(?: мне)? (?:как выгляд\w+|фото\w*|фотку|картинк\w*|"
                   r"изображени\w*)|как выглядит|покажи фото)\s+(?P<obj>.+)$")


def detect(text: str) -> Request | None:
    """Does the question deserve a picture? Pure regex: run on every answer path."""
    low = normalize_command(text, strip_polite=False)
    if _MOLECULE.search(low):
        return Request("molecule", low)
    formula = _FORMULA.search(text.lower().replace("ё", "е"))  # the formula keeps its "-", "^", "/"
    if formula and parse_formula(formula.group(1)):
        return Request("plot", text.lower())
    if _PLOT.search(low):
        return Request("plot", low)
    m = _LOOK.match(low)
    if m:
        obj = m.group("obj").strip()
        if any(k in obj for k in CURVES) or "график" in obj:
            return Request("plot", low)
        return Request("photo", obj)
    return None


_SPOKEN = [
    (r"\b(?:икс|х|x)\b", " x "), (r"\b(?:в квадрате|квадрат|квадрате)\b", "^2 "), (r"\b(?:в кубе|куб|кубе)\b", "^3 "),
    (r"\bв степени\s+", "^"), (r"\bплюс\b", "+"), (r"\bминус\b", "-"),
    (r"\b(?:умножить|умноженное|умноженный|помножить) на\b", "*"), (r"\b(?:делить|деленное|деленный|разделить) на\b", "/"),
    (r"\bкорень(?: квадратный)? из\b", " sqrt "), (r"\bсинус\w*", " sin "), (r"\bкосинус\w*", " cos "),
    (r"\bтангенс\w*", " tan "), (r"\b(?:натуральный )?логарифм\w*", " ln "), (r"\bмодул\w*", " abs "),
    (r"\bэкспонент\w*(?: в степени| от)?", " exp "), (r"\bединиц\w*", "1"), (r"\bот\b", " "),
]


def spoken_formula(text: str) -> str | None:
    """'график x квадрат минус 3 x' / 'график игрек равно синус икс' -> 'x^2 - 3 x' (no model needed)."""
    from assistant.nlu import words_to_numbers

    low = words_to_numbers(text.lower().replace("ё", "е"))
    m = re.search(r"(?:\b(?:y|игрек)\s*(?:=|равно|равен)|\bграфик\w*(?:\s+функци\w*)?)\s*(.+)$", low)
    if not m:
        return None
    t = re.sub(r"^\s*(?:функци\w*\s+)?(?:y|игрек)\s*(?:=|равно|равен)\s*", "", m.group(1))
    for pattern, repl in _SPOKEN:
        t = re.sub(pattern, repl, t)
    t = re.sub(r"[а-я]+", " ", t)                                    # words left over ("мне", "пожалуйста")
    t = re.sub(r"\b(sin|cos|tan|ln|sqrt|abs|exp)\s+([x\d.]+(?:\^\d)?)", r"\1(\2)", t)
    t = re.sub(r"\s+", " ", t).strip(" +*/^,.")
    if "x" not in t or not parse_formula(t):
        return None
    return t


def plot_known(text: str) -> bool:
    """The graph can be built without asking a model: a written or spoken formula, or a named curve."""
    m = _FORMULA.search(text.lower().replace("ё", "е"))
    return bool((m and parse_formula(m.group(1))) or spoken_formula(text) or _curve(normalize_command(text)))


def _curve(text: str) -> tuple[str, str, tuple[float, float]] | None:
    words = text.split()
    for stem, spec in CURVES.items():
        if all(any(w.startswith(part) for w in words) for part in stem.split()):
            return spec
    return None


def _molecule_name(text: str) -> tuple[str, str] | None:
    """(English name for PubChem, caption) from the dictionaries."""
    for stem, (name, caption) in CONCEPTS.items():
        if re.search(rf"\b{stem}\w* (?:связ|кольц|групп)", text):
            return name, caption
    for ru in sorted(MOLECULES, key=len, reverse=True):
        stem = ru.lower()[:-1] if len(ru) > 4 else ru.lower()
        if re.search(rf"\b{re.escape(stem)}", text):
            return MOLECULES[ru], f"{ru[:1].upper() + ru[1:]} — структурная формула."
    return None


async def _ask_local(app: "Assistant", prompt: str) -> str:
    if not app.llm.local.healthy:  # Ollama is off: no picture rather than a 30 s wait
        return ""
    try:
        raw = await app.llm.local.complete([Msg("user", prompt)], max_tokens=40)
    except Exception as exc:
        log.info("Локальная модель для картинки недоступна: %s", exc)
        return ""
    return raw.strip().splitlines()[0].strip(" .`\"'«»") if raw.strip() else ""


async def fetch(req: Request, app: "Assistant") -> Visual | None:
    try:
        if req.kind == "plot":
            return await _plot(req, app)
        async with httpx.AsyncClient(timeout=8, headers=UA, follow_redirects=True) as client:
            if req.kind == "molecule":
                return await _molecule(client, req, app)
            return await _photo(client, req.subject)
    except httpx.HTTPError as exc:
        log.info("Картинка не загрузилась: %s", exc)
        return None


async def _plot(req: Request, app: "Assistant") -> Visual | None:
    m = _FORMULA.search(req.subject)
    spec = None
    if m and parse_formula(m.group(1)):
        expr = m.group(1).strip()
        spec = (expr, "y = " + pretty(expr), (-6.0, 6.0))
    if spec is None and (expr := spoken_formula(req.subject)):
        spec = (expr, "y = " + pretty(expr), (-6.0, 6.0))
    if spec is None:
        spec = _curve(req.subject)
    if spec is None:
        expr = await _ask_local(app, "Запиши функцию из запроса как выражение от x в синтаксисе Python "
                                     "(sin, cos, tan, sqrt, exp, ln, abs, ** для степени). Ответь только выражением, "
                                     f"без «y =». Если функции нет — ответь «нет». Запрос: {req.subject}")
        if expr and parse_formula(expr):
            spec = (expr, "y = " + pretty(expr), (-6.0, 6.0))
    if spec is None:
        return None
    expr, label, xr = spec
    return Visual(label, plot={"expr": expr, "label": label, "x": list(xr)})


def pretty(expr: str) -> str:
    """'x**2 - 3*x' / 'x^2-3x' -> 'x² − 3x' for the graph label."""
    s = re.sub(r"\s*(\*\*|\^)\s*2\b", "²", expr.strip())
    s = re.sub(r"\s*(\*\*|\^)\s*3\b", "³", s)
    s = s.replace("**", "^").replace("*", "·")
    s = re.sub(r"(\d)·(?=[a-z(])", r"\1", s)
    s = re.sub(r"(\d)\s+(?=[a-z(])", r"\1", s)                     # "3 x" -> "3x"
    s = re.sub(r"\s*/\s*", "/", s)
    s = re.sub(r"sqrt\(([\w.^²³]+)\)", r"√\1", s).replace("sqrt", "√")
    s = re.sub(r"\s*([+\-])\s*", r" \1 ", s).replace(" - ", " − ")
    return re.sub(r"^\s*([+\-−])\s+", r"\1", s).strip()


async def _molecule(client: httpx.AsyncClient, req: Request, app: "Assistant") -> Visual | None:
    found = _molecule_name(req.subject)
    caption = ""
    if found:
        name, caption = found
    else:
        name = await _ask_local(app, "Назови английское название вещества из запроса так, как его ищут в PubChem. "
                                     f"Ответь только названием. Если вещества нет — ответь «нет». Запрос: {req.subject}")
        if not name or name.lower() in ("нет", "no", "none") or not re.fullmatch(r"[A-Za-z0-9 ,()'\-+]{2,60}", name):
            return None
    base = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{quote(name)}"
    structure = await _structure(client, base)
    image = b""
    if not structure:  # very large molecules: PubChem's own drawing
        r = await client.get(base + "/PNG?image_size=500x500")
        if r.status_code != 200 or not r.content.startswith(b"\x89PNG"):
            log.info("PubChem не знает «%s»", name)
            return None
        image = r.content
    formula = ""
    try:
        p = await client.get(f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{quote(name)}"
                             "/property/MolecularFormula,IUPACName/JSON")
        prop = p.json()["PropertyTable"]["Properties"][0]
        formula = _subscript(prop.get("MolecularFormula", ""))
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        pass
    title = formula or name
    return Visual(title, caption or f"Структурная формула ({name}).", image, light=bool(image), molecule=structure,
                  source="PubChem")


SYMBOLS = {1: "H", 5: "B", 6: "C", 7: "N", 8: "O", 9: "F", 11: "Na", 12: "Mg", 14: "Si", 15: "P", 16: "S", 17: "Cl",
           19: "K", 20: "Ca", 26: "Fe", 35: "Br", 53: "I"}


async def _structure(client: httpx.AsyncClient, base: str) -> dict:
    """Atoms with 2D coordinates and bonds. PubChem's own picture leaves hydrogens out (ethylene is a bare "="),
    which explains nothing: small molecules are drawn with every atom."""
    r = await client.get(base + "/record/JSON", params={"record_type": "2d"})
    if r.status_code != 200:
        return {}
    try:
        c = r.json()["PC_Compounds"][0]
        elements = c["atoms"]["element"]
        conf = c["coords"][0]["conformers"][0]
        xs, ys = conf["x"], conf["y"]
        b = c.get("bonds") or {"aid1": [], "aid2": [], "order": []}
    except (ValueError, KeyError, IndexError):
        return {}
    heavy = sum(1 for e in elements if e != 1)
    if heavy > 40:
        return {}
    show_h = heavy <= 12
    atoms = [[SYMBOLS.get(e, "?"), float(x), float(y)] for e, x, y in zip(elements, xs, ys)]
    bonds = [[i - 1, j - 1, int(o)] for i, j, o in zip(b["aid1"], b["aid2"], b["order"])]
    # PubChem puts hydrogens at a third of a bond length: move them out to almost a full bond.
    for i, j, _ in bonds:
        for h, a in ((i, j), (j, i)):
            if atoms[h][0] == "H" and atoms[a][0] != "H":
                dx, dy = atoms[h][1] - atoms[a][1], atoms[h][2] - atoms[a][2]
                d = math.hypot(dx, dy) or 1.0
                atoms[h][1], atoms[h][2] = atoms[a][1] + dx / d * 0.85, atoms[a][2] + dy / d * 0.85
    if not show_h:  # a skeletal formula: hydrogens on carbon are implied, those on N and O stay
        keep = [k for k, a in enumerate(atoms) if a[0] != "H" or any(
            (i == k and atoms[j][0] not in ("C", "H")) or (j == k and atoms[i][0] not in ("C", "H")) for i, j, _ in bonds)]
        index = {old: new for new, old in enumerate(keep)}
        atoms = [atoms[k] for k in keep]
        bonds = [[index[i], index[j], o] for i, j, o in bonds if i in index and j in index]
    return {"atoms": atoms, "bonds": bonds, "explicit": show_h}


def _subscript(formula: str) -> str:
    return formula.translate(str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉"))


async def _photo(client: httpx.AsyncClient, subject: str) -> Visual | None:
    """Picture of the best matching Russian Wikipedia article (its lead image) and the article's first sentence."""
    r = await client.get("https://ru.wikipedia.org/w/api.php", params={
        "action": "query", "format": "json", "generator": "search", "gsrsearch": subject, "gsrlimit": 3,
        "prop": "pageimages|extracts", "piprop": "thumbnail", "pithumbsize": 900, "exintro": 1, "explaintext": 1,
        "exchars": 600, "redirects": 1})
    pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
    page = next((p for p in pages if p.get("thumbnail")), None)
    if page is None:
        log.info("В Википедии нет картинки для «%s»", private(subject))
        return None
    img = await client.get(page["thumbnail"]["source"])
    if img.status_code != 200:
        return None
    return Visual(page["title"], first_sentence(page.get("extract", "")), img.content, source="Википедия")


def first_sentence(text: str) -> str:
    """'Жира́ф (лат. Giraffa camelopardalis) — парнокопытное…' -> 'Жираф — парнокопытное…'."""
    text = text.replace("́", "")
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\s*\([^()]*\)", "", text)
    m = re.match(r"(.+?[.!?])(?:\s|$)", text.strip(), re.S)
    return (m.group(1) if m else text.strip())[:240]
