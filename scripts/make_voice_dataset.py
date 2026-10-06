"""Training set for a light Jarvis voice: the cloned voice reads ~2 hours of varied phrases, every clip is checked
by speech recognition, the good ones are written in the LJSpeech layout Piper trains on.

    .venv\\Scripts\\python scripts\\make_voice_dataset.py              # ~2 hours of speech, resumes where it stopped
    .venv\\Scripts\\python scripts\\make_voice_dataset.py --hours 0.05 # a quick test of the whole chain

Close Jarvis first: the clone needs the video memory. Output (data/voice_dataset, not in git):
    wavs/00001.wav ...       22050 Hz mono 16-bit, silence trimmed
    metadata.csv             00001.wav|text  (the text as the voice read it: numbers and Latin already in Russian words)
    metadata_stressed.csv    00001.wav|text with "+" before stressed vowels (what the clone was given)
    rejected.csv             phrases the clone said wrong, with what recognition heard
Phrases: Tatoeba (tatoeba.org, CC BY 2.0 FR) for everyday speech + generated assistant phrases (time, numbers,
money, units, dates, English terms) so the light voice can say what Jarvis says.
"""
from __future__ import annotations

import argparse
import bz2
import csv
import json
import random
import re
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assistant.paths import DATA_DIR  # noqa: E402

OUT = DATA_DIR / "voice_dataset"
TATOEBA = "https://downloads.tatoeba.org/exports/per_language/rus/rus_sentences.tsv.bz2"
SR_OUT = 22050
VOICE = "jarvis-remaster"


# ---------------------------------------------------------------- phrases
def tatoeba_sentences(rng: random.Random, n: int, exclude: set[str] = frozenset(), min_words: int = 4) -> list[str]:
    from assistant.downloads import fetch

    path = fetch(TATOEBA, OUT / "rus_sentences.tsv.bz2", what="фразы Tatoeba (15 МБ)")
    good: list[str] = []
    seen: set[str] = set()
    with bz2.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            s = parts[2].strip()
            words = s.split()
            # Plain Russian prose of a speakable length: no Latin, quotes, brackets or odd symbols.
            if not min_words <= len(words) <= 22 or not re.fullmatch(r"[А-Яа-яЁё0-9 ,.!?;:—–\-]+", s):
                continue
            key = s.lower()
            if key in seen or key in exclude or s[0].islower():
                continue
            seen.add(key)
            good.append(s)
    rng.shuffle(good)
    # Questions and exclamations carry intonation the voice must learn: keep a fair share of them.
    questions = [s for s in good if s.endswith("?")][: n // 4]
    exclaims = [s for s in good if s.endswith("!")][: n // 10]
    rest = [s for s in good if not s.endswith(("?", "!"))][: n - len(questions) - len(exclaims)]
    out = questions + exclaims + rest
    rng.shuffle(out)
    return out


CITIES = ["Москве", "Казани", "Санкт-Петербурге", "Новосибирске", "Сочи", "Екатеринбурге", "Нижнем Новгороде"]
APPS = ["Telegram", "Discord", "Steam", "Chrome", "Яндекс Браузер", "Visual Studio Code", "Spotify", "OBS Studio",
        "Word", "Excel", "Блокнот", "Проводник"]
TERMS = ["REST API", "Python 3.12", "RTX 4060", "GPU", "Wi-Fi", "Bluetooth", "USB", "SSD", "HTTP", "JSON", "Docker",
         "GitHub", "Windows 11", "Linux", "JavaScript", "SQL", "DNS", "IP-адрес", "Ryzen 7", "Core i5", "PlayStation 5"]
THINGS = ["молоко", "хлеб", "яйца", "кофе", "батарейки", "сыр", "яблоки", "зубную пасту", "корм для кота"]
LABELS = ["позвонить маме", "выключить плиту", "проверить почту", "забрать посылку", "выпить таблетку",
          "отправить отчёт", "полить цветы", "встреча с Олегом"]
WEEKDAYS = ["в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]


def assistant_phrases(rng: random.Random, n: int, exclude: set[str] = frozenset()) -> list[str]:
    """What Jarvis actually says: times, numbers, money, units, dates, app names, English terms, "сэр"."""
    r = rng.randint
    c = rng.choice
    templates = [
        lambda: f"Сейчас {r(0, 23)}:{r(0, 59):02d}.",
        lambda: f"Сейчас в {c(CITIES)} {c(['плюс ', 'минус ', ''])}{r(0, 30)} °C, {c(['пасмурно', 'ясно', 'небольшой дождь', 'переменная облачность', 'снег'])}.",
        lambda: f"Ветер {r(1, 15)} м/с, влажность {r(30, 99)}%.",
        lambda: f"Поставил таймер на {r(1, 59)} {c(['минут', 'секунд'])}.",
        lambda: f"Хорошо, через {r(2, 90)} минут напомню: {c(LABELS)}.",
        lambda: f"Будильник {c(['завтра', 'в понедельник', 'по будням'])} в {r(5, 9)}:{c(['00', '15', '30', '45'])}.",
        lambda: f"Курс доллара — {r(70, 105)},{r(10, 99)} ₽, евро — {r(80, 115)},{r(10, 99)} ₽.",
        lambda: f"Это стоит ${r(5, 2000)}, примерно {r(500, 200000)} рублей.",
        lambda: f"На диске C свободно {r(10, 900)} ГБ из {c([256, 512, 952, 1000, 2000])} ГБ.",
        lambda: f"Процессор загружен на {r(1, 100)}%, свободно {r(2, 30)},{r(1, 9)} ГБ оперативной памяти.",
        lambda: f"{r(1, 28)} {c(MONTHS)} {r(1990, 2030)} года.",
        lambda: f"До нового года осталось {r(2, 364)} дней.",
        lambda: f"{r(2, 999)} умножить на {r(2, 99)} — это {r(100, 99000)}.",
        lambda: f"{r(5, 50)}% от {r(100, 10000)} — это {r(5, 5000)}.",
        lambda: f"Открываю {c(APPS)}.",
        lambda: f"Закрыть {c(APPS)} или {c(APPS)}?",
        lambda: f"В списке покупок: {', '.join(rng.sample(THINGS, 3))}.",
        lambda: f"Добавил в список покупок {c(THINGS)}.",
        lambda: f"{c(['Сэр', 'Да, сэр', 'Слушаю, сэр'])}, {c(TERMS)} — {c(['полезная вещь', 'привычный инструмент разработчика', 'часть почти любого компьютера'])}.",
        lambda: f"Видеокарта {c(['RTX 3060', 'RTX 4060 Ti', 'RTX 4070', 'RX 7800 XT'])} стоит около {r(25, 90)} тысяч рублей.",
        lambda: f"Встреча {c(WEEKDAYS)} в {r(9, 20)}:{c(['00', '30'])}.",
        lambda: f"Следующий дождь в {c(CITIES)} начнётся {c(WEEKDAYS)} около {r(6, 22)}:00.",
        lambda: f"Готово, {c(['сэр', 'всё сделано', 'файл сохранён'])}.",
        lambda: f"Нашёл {r(2, 300)} файлов в папке {c(['Загрузки', 'Документы', 'Музыка', 'Рабочий стол'])}.",
        lambda: f"Версия {c(['Python', 'Node.js', 'Docker'])} — {r(1, 20)}.{r(0, 15)}.{r(0, 9)}.",
        lambda: f"{c(['Отличный вопрос', 'Интересно', 'Боюсь, что нет', 'Безусловно', 'Разумеется'])}, сэр.",
        lambda: f"{c(['Не расслышал', 'Не понял'])}, повторите, пожалуйста.",
        lambda: f"Заряд батареи {r(5, 100)}%, {c(['заряжается', 'до отключения около ' + str(r(1, 9)) + ' часов'])}.",
        lambda: f"В {r(1900, 2024)} году население составило {r(2, 900)} миллионов человек.",
        lambda: f"Помодоро: {r(15, 50)} минут работы, затем {r(3, 15)} минут отдыха.",
    ]
    out, seen = [], {s.lower() for s in exclude}
    for _ in range(n * 50):  # the templates can run out of new combinations: stop instead of looping forever
        if len(out) >= n:
            break
        s = c(templates)()
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def phrases(rng: random.Random, n: int) -> list[str]:
    """The phrase list, the same on every resume. A larger --hours appends new phrases (never repeats) to the end,
    so the clips already recorded keep their places; the addition leans to longer sentences (6+ words), which teach
    the voice more intonation per clip."""
    path = OUT / "phrases.txt"
    have = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()] if path.exists() else []
    if len(have) >= n:
        return have
    more = n - len(have)
    rng = random.Random(42 + len(have))  # a new draw for every extension
    used = {s.lower() for s in have}
    own = assistant_phrases(rng, more // 4, used)
    extra = tatoeba_sentences(rng, more - len(own), used, min_words=6 if have else 4) + own
    rng.shuffle(extra)
    if have:
        print(f"Список фраз дополнен: было {len(have)}, добавлено {len(extra)} новых")
    path.write_text("\n".join(have + extra), encoding="utf-8")
    return have + extra


# ---------------------------------------------------------------- checks
def _plain(text: str) -> str:
    from assistant.tts.text_norm import normalize_ru

    t = normalize_ru(text).lower().replace("ё", "е").replace("+", "")
    return re.sub(r"\s+", " ", re.sub(r"[^а-я ]", " ", t)).strip()


def check(said: str, heard: str) -> float:
    """Similarity of what the clone was asked to say and what recognition heard (0..100)."""
    from rapidfuzz import fuzz

    return fuzz.ratio(_plain(said), _plain(heard))


def to_wav(clip, sr_in: int) -> np.ndarray | None:
    import librosa

    audio = np.asarray(clip, dtype=np.float32)
    if audio.size < sr_in * 0.5:
        return None
    audio, _ = librosa.effects.trim(audio, top_db=40)
    audio = librosa.resample(audio, orig_sr=sr_in, target_sr=SR_OUT)
    pad = np.zeros(int(0.12 * SR_OUT), np.float32)
    audio = np.concatenate([pad, audio, pad])
    peak = float(np.max(np.abs(audio))) or 1.0
    return (audio / peak * 0.95 * 32767).astype(np.int16)


def jarvis_running() -> bool:
    import httpx

    try:
        httpx.get("http://127.0.0.1:8770/", timeout=1)
        return True
    except httpx.HTTPError:
        return False


# ---------------------------------------------------------------- main
def main() -> int:
    global OUT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=4.0, help="сколько часов речи собрать всего (уже собранное засчитывается)")
    ap.add_argument("--nfe", type=int, default=32, help="шаги клона: больше — чище (32), меньше — быстрее (16)")
    ap.add_argument("--min-score", type=float, default=90.0, help="порог сходства с распознанным текстом")
    ap.add_argument("--force", action="store_true", help="не проверять, запущен ли Джарвис")
    ap.add_argument("--out", default=str(OUT), help="папка набора")
    args = ap.parse_args()
    OUT = Path(args.out)

    if jarvis_running() and not args.force:
        print("Джарвис запущен и занимает видеопамять. Закройте его (трей -> Выход) и запустите скрипт снова.")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "wavs").mkdir(exist_ok=True)
    rng = random.Random(42)
    target_sec = args.hours * 3600
    todo = phrases(rng, int(args.hours * 2200) + 300)  # ~1700 phrases an hour are kept; spare for rejects

    meta, meta_st, rejected = OUT / "metadata.csv", OUT / "metadata_stressed.csv", OUT / "rejected.csv"
    progress_file = OUT / "progress.json"
    progress = json.loads(progress_file.read_text()) if progress_file.exists() else {"next": 0, "sec": 0.0, "kept": 0, "bad": 0}
    if progress["sec"] >= target_sec:
        print(f"Уже собрано {progress['sec'] / 3600:.2f} ч — готово.")
        return 0

    print("Загружаю клон голоса и распознавание...")
    from assistant.assistant import resample
    from assistant.stt import create_stt
    from assistant.tts.engines import CloneEngine
    from assistant.tts.text_norm import normalize_ru

    CloneEngine.nfe_step = args.nfe
    clone = CloneEngine()
    clone.load(VOICE)
    stt = create_stt("gigaam", "gigaam-v3-e2e-rnnt")

    t0, done_here = time.perf_counter(), 0
    with open(meta, "a", encoding="utf-8", newline="") as fm, open(meta_st, "a", encoding="utf-8", newline="") as fs, \
            open(rejected, "a", encoding="utf-8", newline="") as fr:
        wm, ws, wr = (csv.writer(f, delimiter="|", quoting=csv.QUOTE_NONE, escapechar="\\") for f in (fm, fs, fr))
        for i in range(progress["next"], len(todo)):
            if progress["sec"] >= target_sec:
                break
            text = normalize_ru(todo[i])
            if not text.strip(" .,!?"):
                continue
            stressed = clone._stress(text)
            clip = clone._synth(text, VOICE, 1.0)
            heard = stt.transcribe(resample(clip.samples, clip.sr, 16000))
            score = check(text, heard)
            audio = to_wav(clip.samples, clip.sr) if score >= args.min_score else None
            dur = 0.0 if audio is None else audio.size / SR_OUT
            cps = len(text) / max(dur, 0.1)
            if audio is None or not 6 <= cps <= 25:  # swallowed or stretched speech
                progress["bad"] += 1
                wr.writerow([i, f"{score:.0f}", text, heard])
            else:
                import soundfile as sf

                name = f"{progress['kept'] + 1:05d}"
                sf.write(OUT / "wavs" / f"{name}.wav", audio, SR_OUT, subtype="PCM_16")
                wm.writerow([f"{name}.wav", text])
                ws.writerow([f"{name}.wav", stressed])
                progress["kept"] += 1
                progress["sec"] += dur
            progress["next"] = i + 1
            done_here += 1
            if done_here % 20 == 0:
                for f in (fm, fs, fr):
                    f.flush()
                progress_file.write_text(json.dumps(progress))
                rate = (time.perf_counter() - t0) / done_here
                left = (target_sec - progress["sec"]) / max(progress["sec"] / max(progress["next"], 1), 0.1) * rate
                print(f"{progress['sec'] / 3600:5.2f} / {args.hours:.2f} ч | фраз {progress['kept']}, отброшено "
                      f"{progress['bad']} | {rate:.1f} с на фразу | осталось ~{left / 60:.0f} мин", flush=True)
    progress_file.write_text(json.dumps(progress))
    print(f"Готово: {progress['kept']} фраз, {progress['sec'] / 3600:.2f} ч речи, отброшено {progress['bad']}. "
          f"Папка: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
