"""Router accuracy and speed for a local model: free-form phrases the regex modules do not catch.

    python scripts/router_bench.py qwen3:8b
    python scripts/router_bench.py qwen3:1.7b --repeat 2
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant.brain import Brain  # noqa: E402
from assistant.llm.hub import LlmHub  # noqa: E402
from assistant.skills.base import load_skills  # noqa: E402
from tests.test_skills import FakeApp  # noqa: E402

# phrase -> expected: "skill.action" for commands, or "web" / "chat" / "ignore"
CASES = {
    "хватает ли мне места на компе": "pc.disk",
    "сделай чтобы было слышнее": "media.vol_up|media.speech_volume",
    "мне не слышно ничего": "media.vol_up",
    "вруби что-нибудь послушать": "music.shuffle",
    "сколько у меня там песен": "music.count",
    "что за трек сейчас": "music.now",
    "мне через полчаса надо проверить духовку": "timers.set",
    "не дай мне забыть про звонок через 10 минут": "timers.set",
    "у меня закончилось молоко": "notes.add",
    "что мне надо сделать сегодня": "notes.read",
    "спрячь все окна": "windows.minimize_all",
    "переключи меня на телегу": "windows.switch",
    "запусти мне браузер": "apps.open",
    "глянь, сколько оперативки свободно": "pc.ram",
    "у меня комп тормозит, что его грузит": "pc.cpu",
    "посмотри что творится на экране": "screen.help",
    "мне холодно будет завтра": "weather.forecast",
    "надо ли брать куртку": "weather.forecast",
    "прочти что я выделил": "selection.read",
    "запиши себе что я люблю кофе без сахара": "memory.remember|notes.add",
    "поставь работу на 50 минут": "focus.start",
    "найди в интернете рецепт борща": "search.search",
    "кто сейчас президент франции": "web",
    "сколько стоит биткоин": "web",
    "какой счёт в матче спартака": "web",
    "что такое квантовая запутанность": "chat",
    "посоветуй что почитать": "chat",
    "как мне собраться с мыслями": "chat",
    "жарес": "ignore",
    "нас в музыке": "ignore",
    "ну и что он тебе сказал вчера": "ignore|chat",
    "мда": "ignore",
}


async def run(model: str, repeat: int) -> None:
    app = FakeApp()
    app.cfg.llm.local.model = model
    app.skills = load_skills(app.cfg.skills.enabled)
    for s in app.skills:
        s.setup(app)
    app.llm = LlmHub(app.cfg.llm)
    app.brain = Brain(app, app.skills)
    app.cfg.router.provider = "local"
    app.cfg.router.timeout_sec = 30
    router = app.brain.router
    await router.route("привет", [])  # load the model
    ok, times = 0, []
    for _ in range(repeat):
        for phrase, expected in CASES.items():
            t = time.perf_counter()
            d = await router.route(phrase, [], addressed=not expected.startswith("ignore") or phrase == "жарес")
            times.append(time.perf_counter() - t)
            got = "none" if d is None else d.kind
            if d is not None and d.kind == "commands":
                hits = [app.brain.match_skill(c) for c in d.commands]
                got = ",".join(f"{h[0].name}.{h[1].action}" if h else f"?{c}" for h, c in zip(hits, d.commands))
            good = any(got == e or ("." in e and got.split(",")[0] == e) for e in expected.split("|"))
            ok += good
            print(f"{'OK ' if good else '-- '} {times[-1]:4.2f}s  {phrase:45s} -> {got}  (ждали {expected})")
    n = len(CASES) * repeat
    print(f"\n{model}: точность {ok}/{n} ({ok / n:.0%}), медиана {statistics.median(times):.2f} с, "
          f"90% {sorted(times)[int(0.9 * len(times))]:.2f} с")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    asyncio.run(run(args.model, args.repeat))


if __name__ == "__main__":
    main()
