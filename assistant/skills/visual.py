"""Picture questions: "как выглядит гипербола", "молекула кофеина", "покажи график y = x^2", "как выглядит жираф".

The picture opens in its own window (assistant/visuals.py) while the voice explains it in a couple of sentences;
key facts go to the card window next to it. Questions that only mention such a thing ("что значит двойная связь")
are answered as usual, and the brain adds the same picture to the answer.
"""
from __future__ import annotations

import re

from assistant import visuals
from assistant.skills.base import Intent, Reply, Skill

_ASK = re.compile(
    r"^(?:а\s+)?(?:как выгляд(?:ит|ят|ел|ела|ело|ели)\s+.+"
    r"|(?:покажи|нарисуй|построй|начерти|изобрази)(?: мне)? (?:как выгляд\w+|фото\w*|фотку|картинк\w*|изображени\w*|"
    r"график\w*|молекул\w*|структур\w*|структурн\w* формул\w*|формул\w* (?:молекул\w*|вещества)|строение)\s*.+"
    r"|(?:молекула|структурная формула|строение молекулы|график функции|график)\s+.+)$")


class VisualSkill(Skill):
    name = "visual"
    title = "Картинка к объяснению"
    examples = [
        'как выглядит <что>',
        'покажи график <функция>',
        'молекула <вещество>',
    ]

    def match(self, text: str) -> Intent | None:
        if not _ASK.match(text):
            return None
        req = visuals.detect(text)
        if req is None:
            return None
        if req.kind == "plot" and not visuals.plot_known(text):
            return None  # "график погоды", "график отпусков": not a function, other modules or the router decide
        return Intent(self.name, "show", {"kind": req.kind}, text)

    async def handle(self, intent: Intent) -> Reply:
        question = intent.raw or intent.text
        req = self.app.show_visual_for(question)
        # The voice explains what is on the picture; a stable fact, so no web search and the answer is cached.
        return self.app.brain.answer(question, fresh=False, picture=req.kind if req else "")


def create() -> Skill:
    return VisualSkill()
