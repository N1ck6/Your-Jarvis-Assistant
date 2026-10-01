"""Russian speech background for scripts/wake_bench.py: news-like sentences full of words that sound close to
"Джарвис" (Дарвин, Гарвард, жарко, Джордж...), read by five Silero voices. Written to a WAV file you choose.

    python scripts/make_speech_background.py bench_speech.wav --minutes 30
"""
from __future__ import annotations

import argparse
import random
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NAMES = ["Дарвин", "Гарвард", "Джордж", "Джерри", "Чарльз", "Харви", "Харрис", "Джексон", "Джеймс", "Джонни",
         "Жанна", "Жора", "Ярослав", "Арвид", "Джастин", "Чарли", "Джейсон", "Джимми", "Давид", "Гарри"]
WORDS = ["жарко", "жаркий", "жалюзи", "ярмарка", "жар-птица", "джинсы", "джаз", "давайте", "жарить", "жалость",
         "журавль", "ярко", "жребий", "жирафы", "джунгли", "ржавый", "Аркадий", "сервис", "арбуз", "Жигули"]
TEMPLATES = [
    "Сегодня {name} выступил с докладом, а в зале было {word}.",
    "По данным синоптиков, завтра будет {word}, а {name} советует взять зонт.",
    "Учёный {name} из университета рассказал, почему {word} влияет на урожай.",
    "В эфире новости: {name} объявил о новом проекте, о нём уже говорят как про {word}.",
    "Слушайте, {name}, давайте обсудим это позже, сейчас слишком {word}.",
    "Курс рубля сегодня снова изменился, а {name} считает, что это {word}.",
    "Матч закончился вничью, лучшим игроком признан {name}.",
    "Вчера на рынке открылась {word}, и туда пришёл {name} с семьёй.",
    "Теория, которую предложил {name}, объясняет, откуда берётся {word}.",
    "{name}, ты слышал, что говорят про {word}? Говорят, это надолго.",
    "Ведущий спросил гостя: {name}, как вы относитесь к тому, что стало так {word}?",
    "Пробки в городе достигли восьми баллов, водителям советуют объезжать центр.",
    "В выходные ожидается до двадцати пяти градусов тепла и небольшой дождь.",
    "Сериал о жизни {name} выйдет на экраны в следующем месяце.",
]
SPEAKERS = ["aidar", "baya", "kseniya", "xenia", "eugene"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    from assistant.tts.engines import SileroEngine
    from assistant.tts.text_norm import normalize_ru

    rnd = random.Random(args.seed)
    engine = SileroEngine()
    engine.load("eugene")
    total, chunks = 0.0, []
    while total < args.minutes * 60:
        text = rnd.choice(TEMPLATES).format(name=rnd.choice(NAMES), word=rnd.choice(WORDS))
        clip = engine._synth(normalize_ru(text), rnd.choice(SPEAKERS), rnd.uniform(0.9, 1.15))
        pcm = (np.clip(clip.samples[::3], -1, 1) * 32767 * rnd.uniform(0.3, 0.9)).astype(np.int16)
        chunks += [pcm, np.zeros(int(16000 * rnd.uniform(0.2, 1.2)), np.int16)]
        total += len(pcm) / 16000 + 0.7
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.concatenate(chunks).tobytes())
    print(f"{args.out}: {total / 60:.0f} мин речи")
    return 0


if __name__ == "__main__":
    sys.exit(main())
