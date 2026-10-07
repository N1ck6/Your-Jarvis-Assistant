"""Barge-in benchmark: does Jarvis hear "Джарвис, …" said over his own speech?

Jarvis speaks a long answer (his own voice, as the microphone hears it from the speakers, at several loudness
levels); other voices say commands over it at random moments. The same VAD -> Vosk -> capture -> GigaAM path the
assistant uses decides whether the command was caught and recognized.

    python scripts/bargein_bench.py               # 40 commands per echo level
    python scripts/bargein_bench.py --n 80 --echo 0.5 1 2

--echo: loudness of Jarvis's voice at the microphone relative to the user's voice (RMS ratio): 0.5 — quiet speakers
or a headset, 1 — the same loudness, 2 — loud speakers next to the microphone.
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant.assistant import resample  # noqa: E402
from assistant.audio import aec as aec_mod  # noqa: E402
from assistant.audio.aec import EchoCanceller, Reference  # noqa: E402
from assistant.audio.listener import ListenerCore, ListenerEvents  # noqa: E402
from assistant.audio.vad import FRAME, SileroVad  # noqa: E402
from assistant.audio.wake import VoskWake  # noqa: E402
from assistant.config import load_settings  # noqa: E402
from assistant.nlu import find_wake, normalize_command  # noqa: E402
from rapidfuzz import fuzz  # noqa: E402

ANSWER = ("Двойная связь — это связь между двумя атомами, в которой участвуют две общие пары электронов. "
          "Одна из них образует сигма-связь вдоль оси между ядрами, вторая — пи-связь над и под этой осью. "
          "Поэтому вокруг двойной связи молекула не может свободно вращаться, и у неё появляются изомеры. "
          "Классический пример — этилен: два атома углерода, каждый связан ещё с двумя атомами водорода. "
          "Двойная связь короче и прочнее одинарной, но пи-связь легко разрывается, поэтому алкены активно "
          "вступают в реакции присоединения: с водородом, галогенами и водой.")
COMMANDS = ["Джарвис, какая погода завтра", "Джарвис, поставь таймер на пять минут", "Джарвис, стоп, открой телеграм",
            "Джарвис, сделай тише", "Джарвис, включи музыку", "Джарвис, который час",
            "Джарвис, найди в яндексе рецепт борща", "Джарвис, напомни через десять минут позвонить маме"]
USER_VOICES = ["aidar", "baya", "kseniya", "xenia", "eugene"]


def pcm16(audio: np.ndarray) -> np.ndarray:
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16)


def translit(text: str) -> str:
    """Latin app names from the recognizer as the commands spell them ("telegram" -> "телеграм")."""
    for lat, cyr in (("telegram", "телеграм"), ("youtube", "ютуб")):
        text = text.replace(lat, cyr)
    return text


def rms(a: np.ndarray) -> float:
    return float(np.sqrt(np.mean(a.astype(np.float64) ** 2)) + 1e-9)


def room(x: np.ndarray, rng: np.random.Generator, delay_ms: float = 70) -> np.ndarray:
    """What the microphone hears from the speakers: a delay, reflections, speaker distortion."""
    ir = np.zeros(int(0.25 * 16000))
    d = int(delay_ms * 16)
    ir[d] = 0.8
    ir[d:] += rng.standard_normal(len(ir) - d) * np.exp(-np.arange(len(ir) - d) / 600) * 0.08
    y = np.convolve(x, ir)[: len(x)]
    return (np.tanh(1.6 * y) / np.tanh(1.6)).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=40, help="команд на каждый уровень эха")
    ap.add_argument("--echo", type=float, nargs="+", default=[0.5, 1.0, 2.0])
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-aec", action="store_true", help="без эхоподавления (как было раньше)")
    ap.add_argument("--verbose", action="store_true", help="показать каждую неудачу")
    ap.add_argument("--idle", action="store_true", help="без команд: ложные перебивания и приглушения на речи Джарвиса")
    ap.add_argument("--duck", type=float, default=0.3, help="громкость Джарвиса, когда его перебивают (1 — не убавлять)")
    args = ap.parse_args()
    cfg = load_settings()
    rng = random.Random(args.seed)
    nprng = np.random.default_rng(args.seed)

    from assistant.stt import create_stt
    from assistant.tts.engines import PiperEngine, SileroEngine

    print("Синтез речи Джарвиса и команд...")
    piper, silero = PiperEngine(), SileroEngine()
    piper.load("ru_RU-jarvis-medium")
    jarvis = piper.synth(ANSWER, "ru_RU-jarvis-medium")
    echo16 = resample(jarvis.samples, jarvis.sr, 16000)
    silero.load("eugene")
    commands = []
    for i in range(len(COMMANDS) * len(USER_VOICES)):
        text, voice = COMMANDS[i % len(COMMANDS)], USER_VOICES[i % len(USER_VOICES)]
        clip = silero.synth(text, voice)
        a = resample(clip.samples, clip.sr, 16000)
        loud = np.nonzero(np.abs(a) > 0.01)[0]
        commands.append((text, a[loud[0]:loud[-1] + 1]))
    stt = create_stt(cfg.stt.engine, cfg.stt.model, cfg.stt.quantization)

    for level in args.echo:
        caught = understood = 0
        for k in range(args.n):
            text, cmd = commands[rng.randrange(len(commands))]
            start = rng.uniform(1.0, len(echo16) / 16000 - 4.0)
            lead, tail = np.zeros(16000, np.float32), np.zeros(int(16000 * 2.5), np.float32)
            played = np.concatenate([lead, echo16 * 0.5, tail])             # what Jarvis sends to the speakers
            heard_echo = room(played, nprng)
            echo = heard_echo * (level * rms(cmd) / rms(heard_echo[16000:16000 + len(echo16)]))  # at the microphone
            user = np.zeros_like(echo)
            ref = Reference()
            ref.played_sec = 60.0          # in the app the canceller has long adapted to the room
            speech = ref.stream()
            aec = None if args.no_aec else EchoCanceller(ref)
            s = int((start + 1.0) * 16000)
            if not args.idle:
                user[s:s + len(cmd)] += cmd[: len(user) - s]
            ducks = []
            events = []
            ev = ListenerEvents(on_wake=lambda: events.append("wake"),
                                on_stop_word=lambda w, a: events.append(("stop", w)),
                                on_speech_start=lambda: None,
                                on_utterance=lambda u: events.append(("utt", u)),
                                on_await_timeout=lambda: None,
                                on_talk_over=lambda on: (duck.__setitem__("want", args.duck if on else 1.0),
                                                         ducks.append(on),
                                                         on and args.verbose and print(f"   приглушение на {cur['t']:.1f} с")))
            core = ListenerCore(SileroVad(cfg.audio.sample_rate),
                                VoskWake(cfg.resolve(cfg.wake.model), cfg.wake.phrases, cfg.wake.decoys, cfg.wake.stop_words),
                                ev, sr=16000, vad_threshold=cfg.audio.vad_threshold,
                                end_silence_ms=cfg.audio.end_silence_ms, wake_grace_ms=cfg.audio.wake_grace_ms,
                                pre_roll_ms=cfg.audio.pre_roll_ms)
            speaking_until = int((1.0 + len(echo16) / 16000) * 16000)
            stop_at = None
            wake_at = 0
            duck = {"want": 1.0, "gain": 1.0, "lag": []}
            cur = {"t": 0.0}
            for i in range(0, len(user) - FRAME + 1, FRAME):
                # Jarvis stops talking when the wake word is heard; the speaker falls silent ~0.15 s later.
                if stop_at is None and "wake" in events:
                    stop_at = i + int(0.15 * 16000)
                    wake_at = i
                talking = i < speaking_until and (stop_at is None or i < stop_at)
                core.assistant_speaking = talking
                cur["t"] = i / 16000
                aec_mod.clock = lambda: cur["t"]   # the replay runs faster than real time
                # Jarvis's voice follows the requested volume ~50 ms later (the output buffer).
                duck["lag"].append(duck["want"])
                if len(duck["lag"]) > 2:
                    duck["gain"] = duck["lag"].pop(0)
                g = duck["gain"]
                if talking:
                    speech.write(played[i:i + FRAME] * g, 16000)
                frame = pcm16(user[i:i + FRAME] + (echo[i:i + FRAME] * g if talking else 0))
                if aec is not None:
                    core.process(aec.process(frame, captured_at=cur["t"]), raw=frame)
                else:
                    core.process(frame)
            utts = [e[1] for e in events if isinstance(e, tuple) and e[0] == "utt"]
            if args.idle:
                caught += "wake" in events
                understood += sum(ducks)   # times Jarvis turned himself down for nothing
                continue
            if "wake" in events and utts:
                caught += 1
                heard = stt.transcribe(utts[0].audio)
                span = find_wake(heard, cfg.wake.phrases, cfg.wake.verify_ratio)
                after = normalize_command(heard[span[1]:] if span else heard)
                want = normalize_command(text.split(",", 1)[1])
                if fuzz.partial_ratio(want, translit(after)) >= 75:
                    understood += 1
                elif args.verbose:
                    au = utts[0].audio
                    prof = " ".join(f"{np.sqrt(np.mean(au[j:j + 8000] ** 2)):.2f}" for j in range(0, len(au), 8000))
                    print(f"   мимо: сказано «{text}», услышано «{heard}» ({len(au) / 16000:.1f} с, wake на "
                          f"{wake_at / 16000:.1f} с, команда {start + 1:.1f}–{start + 1 + len(cmd) / 16000:.1f} с; "
                          f"громкость по 0,5 с: {prof})")
            elif args.verbose:
                print(f"   не услышал: «{text}» на {start:.1f} с")
        if args.idle:
            print(f"без команд, эхо x{level:.1f}: ложных «Джарвис» {caught}/{args.n} прогонов по "
                  f"{len(echo16) / 16000:.0f} с, ложных приглушений {understood}", flush=True)
            continue
        print(f"{'без AEC' if args.no_aec else 'AEC'}{'' if args.no_aec or args.duck >= 1 else ' + приглушение'} эхо x{level:.1f}: услышал «Джарвис» {caught}/{args.n} ({100 * caught / args.n:.0f}%), "
              f"команда распознана {understood}/{args.n} ({100 * understood / args.n:.0f}%)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
