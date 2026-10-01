"""False wake-word benchmark: feeds hours of background audio (music, podcasts, films) through the same
VAD -> Vosk -> GigaAM check the assistant uses and counts how often "Джарвис" fires by mistake.

    python scripts/wake_bench.py ~/Music --minutes 60
    python scripts/wake_bench.py film.mp4 podcast.mp3 --decoys "джон,джей,давай"

Nothing is recorded or saved; audio is decoded in memory.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant.audio.listener import ListenerCore, ListenerEvents  # noqa: E402
from assistant.audio.vad import FRAME, SileroVad  # noqa: E402
from assistant.audio.wake import VoskWake  # noqa: E402
from assistant.config import load_settings  # noqa: E402
from assistant.nlu import find_wake  # noqa: E402

AUDIO = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus", ".wma", ".mp4", ".mkv", ".webm"}


def files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(lambda x: Path(x).expanduser(), paths):
        out += [f for f in sorted(p.rglob("*")) if f.suffix.lower() in AUDIO] if p.is_dir() else [p]
    return out


def decode(path: Path) -> np.ndarray | None:
    import miniaudio

    try:
        d = miniaudio.decode_file(str(path), output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=16000)
    except Exception as exc:  # video containers miniaudio cannot read
        print(f"  пропускаю {path.name}: {exc}")
        return None
    return np.frombuffer(d.samples, dtype=np.int16)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--minutes", type=float, default=60, help="сколько минут фона прогнать (случайные файлы)")
    ap.add_argument("--decoys", default="", help="свои приманки через запятую вместо настроек")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--recall", type=int, default=0,
                    help="ещё проверить N синтезированных фраз «Джарвис, …»: сколько из них услышано")
    args = ap.parse_args()

    cfg = load_settings()
    decoys = [d.strip() for d in args.decoys.split(",") if d.strip()] or cfg.wake.decoys
    from assistant.stt import create_stt

    stt = create_stt(cfg.stt.engine, cfg.stt.model, cfg.stt.quantization)
    pool = files(args.paths)
    random.Random(args.seed).shuffle(pool)
    triggers: list[np.ndarray] = []
    events = ListenerEvents(on_wake=lambda: None, on_stop_word=lambda w, a: None, on_speech_start=lambda: None,
                            on_utterance=lambda u: triggers.append(u.audio), on_await_timeout=lambda: None)
    core = ListenerCore(SileroVad(), VoskWake(cfg.resolve(cfg.wake.model), cfg.wake.phrases, decoys, cfg.wake.stop_words),
                        events, vad_threshold=cfg.audio.vad_threshold, end_silence_ms=cfg.audio.end_silence_ms,
                        wake_grace_ms=cfg.audio.wake_grace_ms, pre_roll_ms=cfg.audio.pre_roll_ms)
    total = 0.0
    t0 = time.perf_counter()
    for path in pool:
        if total >= args.minutes * 60:
            break
        pcm = decode(path)
        if pcm is None:
            continue
        pcm = pcm[: int((args.minutes * 60 - total) * 16000)]
        for i in range(0, len(pcm) - FRAME + 1, FRAME):
            core.process(pcm[i:i + FRAME])
        total += len(pcm) / 16000
        print(f"  {path.name[:50]:50s} {len(pcm) / 16000 / 60:5.1f} мин, срабатываний Vosk: {len(triggers)}")
    hours = total / 3600
    false_wakes = []
    for audio in triggers:
        text = stt.transcribe(audio)
        if text and find_wake(text, cfg.wake.phrases, cfg.wake.verify_ratio):
            false_wakes.append(text)
    print(f"\nФон: {total / 60:.0f} мин ({len(pool)} файлов в наборе), обработка {time.perf_counter() - t0:.0f} с")
    print(f"Vosk сработал: {len(triggers)} раз ({len(triggers) / max(hours, 1e-9):.1f} в час)")
    print(f"После проверки GigaAM ложных пробуждений: {len(false_wakes)} ({len(false_wakes) / max(hours, 1e-9):.1f} в час)")
    for t in false_wakes:
        print("   ", t)
    if args.recall:
        print(f"\nУслышано «Джарвис» в синтезированных фразах: {recall(cfg, decoys, stt, args.recall, args.seed)}")
    return 0


COMMANDS = ["какая погода завтра", "включи музыку", "поставь таймер на пять минут", "открой телеграм", "который час",
            "сделай потише", "что на экране", "напомни через час позвонить маме", "сколько стоит биткоин", "стоп"]


def recall(cfg, decoys: list[str], stt, n: int, seed: int) -> str:
    """Share of "Джарвис, <command>" phrases (five Silero voices, pauses after the name) that wake Jarvis."""
    from assistant.tts.engines import SileroEngine

    rnd = random.Random(seed)
    engine = SileroEngine()
    engine.load("eugene")
    heard = 0
    for i in range(n):
        name = rnd.choice(["Джарвис", "Джарвис,", "Джарвис."])
        clip = engine._synth(f"{name} {rnd.choice(COMMANDS)}.", rnd.choice(["aidar", "baya", "kseniya", "xenia", "eugene"]),
                             rnd.uniform(0.9, 1.15))
        pcm = (np.clip(clip.samples[::3], -1, 1) * 32767 * rnd.uniform(0.3, 0.9)).astype(np.int16)
        pcm = np.concatenate([np.zeros(16000, np.int16), pcm, np.zeros(40000, np.int16)])
        utts: list[np.ndarray] = []
        events = ListenerEvents(on_wake=lambda: None, on_stop_word=lambda w, a: None, on_speech_start=lambda: None,
                                on_utterance=lambda u: utts.append(u.audio), on_await_timeout=lambda: None)
        core = ListenerCore(SileroVad(), VoskWake(cfg.resolve(cfg.wake.model), cfg.wake.phrases, decoys, cfg.wake.stop_words),
                            events, vad_threshold=cfg.audio.vad_threshold, end_silence_ms=cfg.audio.end_silence_ms,
                            wake_grace_ms=cfg.audio.wake_grace_ms, pre_roll_ms=cfg.audio.pre_roll_ms)
        for j in range(0, len(pcm) - FRAME + 1, FRAME):
            core.process(pcm[j:j + FRAME])
        if any(find_wake(stt.transcribe(u), cfg.wake.phrases, cfg.wake.verify_ratio) for u in utts):
            heard += 1
    return f"{heard} из {n} ({heard / max(n, 1):.0%})"


if __name__ == "__main__":
    sys.exit(main())
