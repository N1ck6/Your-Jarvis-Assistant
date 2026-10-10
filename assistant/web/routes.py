"""Site routes: how to get to results on a site in one step instead of five.

Built-in: search addresses of popular Russian sites and how to sort there. Learned: after every finished task the
agent remembers the search address it ended up on and a one-line lesson (data/web_routes.json), so the next time
the planner starts right there.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlsplit, urlunsplit, urlencode

from assistant.paths import DATA_DIR

log = logging.getLogger("web")
FILE = DATA_DIR / "web_routes.json"


@dataclass
class Site:
    domain: str
    name: str
    aliases: tuple[str, ...]
    search: str = ""                 # address with {q}
    hints: list[str] = field(default_factory=list)
    kind: str = "shop"               # shop | video | text | search


BUILTIN = [
    Site("ozon.ru", "Озон", ("озон", "ozon"), "https://www.ozon.ru/search/?text={q}&from_global=true",
         ["сортировка: &sorting=price (дешевле), &sorting=rating (рейтинг), &sorting=score (популярные)",
          "цена: &currency_price=1000.000%3B3000.000 (от;до)"]),
    Site("wildberries.ru", "Wildberries", ("вайлдберриз", "вайлдбериз", "валдберис", "валберис", "вб", "wildberries",
                                           "вилдберис", "wb"),
         "https://www.wildberries.ru/catalog/0/search.aspx?search={q}",
         ["сортировка: &sort=priceup (дешевле), &sort=rate (рейтинг), &sort=popular",
          "цена: &priceU=100000%3B300000 (в копейках от;до)"]),
    Site("market.yandex.ru", "Яндекс Маркет", ("яндекс маркет", "маркет", "маркете"),
         "https://market.yandex.ru/search?text={q}",
         ["сортировка: &how=aprice (дешевле), &how=rating, &how=opinions (отзывы)", "цена: &pricefrom=1000&priceto=3000"]),
    Site("megamarket.ru", "Мегамаркет", ("мегамаркет",), "https://megamarket.ru/catalog/?q={q}"),
    Site("avito.ru", "Авито", ("авито", "avito"), "https://www.avito.ru/all?q={q}",
         ["сортировка: &s=1 (дешевле), &s=104 (новые)", "цена: &pmin=1000&pmax=3000"]),
    Site("aliexpress.ru", "AliExpress", ("алиэкспресс", "алиэкспрессе", "aliexpress"),
         "https://aliexpress.ru/wholesale?SearchText={q}"),
    Site("dns-shop.ru", "DNS", ("днс", "dns"), "https://www.dns-shop.ru/search/?q={q}"),
    Site("citilink.ru", "Ситилинк", ("ситилинк",), "https://www.citilink.ru/search/?text={q}"),
    Site("lamoda.ru", "Lamoda", ("ламода", "lamoda"), "https://www.lamoda.ru/catalogsearch/result/?q={q}"),
    Site("youtube.com", "YouTube", ("ютуб", "ютубе", "youtube"), "https://www.youtube.com/results?search_query={q}",
         ["фильтр по дате: &sp=CAI%253D (новые)"], "video"),
    Site("rutube.ru", "Rutube", ("рутуб", "рутубе", "rutube"), "https://rutube.ru/search/?query={q}", [], "video"),
    Site("vkvideo.ru", "VK Видео", ("вк видео",), "https://vkvideo.ru/?q={q}", [], "video"),
    Site("kinopoisk.ru", "Кинопоиск", ("кинопоиск",), "https://www.kinopoisk.ru/index.php?kp_query={q}", [], "text"),
    Site("habr.com", "Хабр", ("хабр", "хабре", "habr"), "https://habr.com/ru/search/?q={q}", [], "text"),
    Site("ru.wikipedia.org", "Википедия", ("википедия", "википедии", "вики"),
         "https://ru.wikipedia.org/w/index.php?search={q}", [], "text"),
    Site("hh.ru", "hh.ru", ("хедхантер", "hh", "эйчэйч"), "https://hh.ru/search/vacancy?text={q}", [], "text"),
    Site("2gis.ru", "2ГИС", ("2гис", "дубльгис"), "https://2gis.ru/search/{q}", [], "text"),
    Site("yandex.ru", "Яндекс", ("яндекс", "яндексе"), "https://yandex.ru/search/?text={q}", [], "search"),
    Site("google.com", "Google", ("гугл", "гугле", "google"), "https://www.google.com/search?q={q}", [], "search"),
]


def _norm_domain(host: str) -> str:
    host = host.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host


class Routes:
    def __init__(self, path: Path = FILE) -> None:
        self.path = path
        self.learned: dict[str, dict] = {}
        try:
            self.learned = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.learned, ensure_ascii=False, indent=1), encoding="utf-8")

    def mentioned(self, text: str) -> list[Site]:
        """Sites named in the request ("на озоне", "на вб")."""
        low = " " + re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е")) + " "
        out = [s for s in BUILTIN if any(f" {a} " in low or f" {a}е " in low for a in s.aliases)]
        for domain, item in self.learned.items():
            if domain in text.lower() and all(s.domain != domain for s in out):
                out.append(Site(domain, domain, (domain,), item.get("search", ""), item.get("hints", [])))
        return out

    def site(self, domain: str) -> Site | None:
        domain = _norm_domain(domain)
        return next((s for s in BUILTIN if domain == s.domain or domain.endswith("." + s.domain)), None)

    def search_url(self, domain: str, query: str) -> str:
        item = self.learned.get(_norm_domain(domain), {})
        site = self.site(domain)
        template = item.get("search") or (site.search if site else "")
        return template.replace("{q}", quote_plus(query)) if template else ""

    def describe(self, sites: list[Site]) -> str:
        """For the planner prompt."""
        lines = []
        for s in sites:
            learned = self.learned.get(s.domain, {})
            search = learned.get("search") or s.search
            hints = [*s.hints, *learned.get("hints", [])]
            line = f"- {s.name} ({s.domain})" + (f": поиск {search}" if search else "")
            if hints:
                line += "; " + "; ".join(hints[-6:])
            lines.append(line)
        return "\n".join(lines)

    def learn(self, visited: list[str], query: str, lesson: str = "") -> None:
        """After a finished task: the address that held the query becomes the site's search route."""
        changed = False
        q = query.strip().lower()
        for url in visited:
            parts = urlsplit(url)
            domain = _norm_domain(parts.hostname or "")
            if not domain:
                continue
            params = parse_qsl(parts.query, keep_blank_values=True)
            hit = [i for i, (_k, v) in enumerate(params) if q and v.strip().lower() == q]
            if hit and not self.site(domain):
                i = hit[0]
                kept = [(k, v) for j, (k, v) in enumerate(params) if j == i or k in ("from_global",)]
                template = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))
                template = template.replace(quote_plus(params[i][1]), "{q}")
                item = self.learned.setdefault(domain, {})
                if item.get("search") != template:
                    item["search"] = template
                    item["ts"] = time.time()
                    changed = True
                    log.info("Запомнил поиск на %s: %s", domain, template)
        if lesson and visited:
            domain = _norm_domain(urlsplit(visited[-1]).hostname or "")
            if domain:
                item = self.learned.setdefault(domain, {})
                hints = [h for h in item.get("hints", []) if h != lesson]
                item["hints"] = (hints + [lesson[:200]])[-5:]
                item["ts"] = time.time()
                changed = True
        if changed:
            self._save()
