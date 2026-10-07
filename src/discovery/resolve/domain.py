"""Company name -> official web domain, to seed the resolver.

A name guessed into a domain misses what the company really uses (Eli Lilly
is lilly.com, not elililly.com); a lookup service knows. `official_domain`
asks the `[discovery].domain_lookup_urls` suggesters, then Wikidata, and
accepts only a suggestion that names the same company.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from src import config
from src.match import names
from src.net import http
from src.net.util import JSON, cache_dir, dig, host_of, json_cache_get, json_cache_put
from src.runstate import per_run

# Dropped before two names are compared: legal forms, never industry words
# ("Precision BioSciences" is not "Precision Nutrition").
_LEGAL = frozenset({"inc", "llc", "ltd", "corp", "corporation", "co",
                    "company", "plc", "gmbh", "and"})

# The domains.json cache, read once per run and written through.
_CACHE = per_run(lambda: json_cache_get(cache_dir("domains.json"), float("inf")) or {})


def _words(name: str | None) -> list[str]:
    return [w for w in names.name_words(name) if w not in _LEGAL]


def _bare(domain: str | None) -> str:
    """The host of a domain or URL, lowercase, without `www.`.

    >>> _bare("https://www.Lilly.com/en"), _bare("lilly.com"), _bare("localhost")
    ('lilly.com', 'lilly.com', '')
    """
    d = (domain or "").strip().lower()
    host = host_of(d if "//" in d else "//" + d).removeprefix("www.")
    return host if "." in host else ""


def pick_domain(name: str, suggestions: Iterable[tuple[str | None, str | None]]
                ) -> str | None:
    """The domain of the (label, domain) suggestion whose label is `name`
    less legal suffixes, a generic TLD (.com, .org...) before a country
    code's; None when none is.

    >>> pick_domain("Eli Lilly and Company", [("Eli Lilly and", "lilly.com")])
    'lilly.com'
    >>> pick_domain("Fujifilm Diosynth Biotechnologies",
    ...             [("FUJIFILM Diosynth Biotechnologies", "fujifilmdiosynth.com")])
    'fujifilmdiosynth.com'
    >>> pick_domain("Charles River Laboratories",
    ...             [("Charles River Laboratories", "criver.com")])
    'criver.com'
    >>> pick_domain("Precision BioSciences",
    ...             [("Precision Nutrition", "precisionnutrition.com")])
    >>> pick_domain("Alera Labs", [("Alera Group", "aleragroup.com")])
    >>> pick_domain("Locus Biosciences", [("Locus", "locusmag.com")])
    >>> pick_domain("Caidya", [("Caidya", "caidya.cn"), ("Caidya", "caidya.com")])
    'caidya.com'
    """
    generic = (".com", ".org", ".bio", ".health", ".ai", ".io")
    want = _words(name)
    found = [d for label, dom in suggestions
             if want and _words(label) == want and (d := _bare(dom))]

    def specific_first(d: str) -> bool:
        return not d.endswith(generic)
    return min(found, key=specific_first, default=None)


async def _suggested(name: str) -> tuple[str | None, bool]:
    """(domain, answered) from the `[discovery].domain_lookup_urls`
    suggesters, the legal-stripped name asked first and the industry-stripped
    one second. `answered` is False when no suggester replied."""
    answered = False
    queries = dict.fromkeys(q for q in (" ".join(_words(name)),
                                        names.strip_suffixes(name)) if q)
    for query in queries:
        for template in config.DISCOVERY_DOMAIN_LOOKUP_URLS:
            data = await http.get_json(template.format(q=quote(query)), "domain lookup")
            if not isinstance(data, list):
                continue
            answered = True
            hit = pick_domain(name, [(_str(s.get("name")), _str(s.get("domain")))
                                     for s in data if isinstance(s, dict)])
            if hit:
                return hit, True
    return None, answered


def _str(v: JSON) -> str | None:
    return v if isinstance(v, str) else None


async def _wikidata(name: str) -> tuple[str | None, bool]:
    """(domain, answered) from Wikidata: the official website (P856) of an
    entity whose English label passes `pick_domain`'s match."""
    api, label = config.WIKIDATA_API, "wikidata"
    found = await http.get_json(api, label, params={
        "action": "wbsearchentities", "search": names.strip_parentheticals(name),
        "language": "en", "limit": 3, "format": "json"})
    if not isinstance(found, dict):
        return None, False
    search = found.get("search")
    labels: dict[str, str | None] = {str(e["id"]): _str(e.get("label")) for e in search
              if isinstance(e, dict) and e.get("id")} if isinstance(search, list) else {}
    if not any(_words(lbl) == _words(name) for lbl in labels.values()):
        return None, True
    data = await http.get_json(api, label, params={
        "action": "wbgetentities", "ids": "|".join(labels), "props": "claims",
        "format": "json"})
    if not isinstance(data, dict):
        return None, False
    sites = []
    entities = dig(data, "entities")
    for qid, entity in entities.items() if isinstance(entities, dict) else []:
        claims = dig(entity, "claims", "P856")
        for claim in claims if isinstance(claims, list) else []:
            value = dig(claim, "mainsnak", "datavalue", "value")
            if isinstance(value, str):
                sites.append((labels.get(qid), value))
    return pick_domain(name, sites), True


async def official_domain(name: str) -> str | None:
    """`name`'s official domain without `www.`, or None.

    Cached in `.cache/domains.json`: a domain 30 days, a miss 7. A lookup
    that never got an answer is not cached.
    """
    key = names.name_key(name)
    if not (key and _words(name)):
        return None
    cache: dict[str, Any] = _CACHE()
    now = time.time()
    got = cache.get(key)
    if got and now - got["at"] < (30 if got["domain"] else 7) * 86400:
        cached: str | None = got["domain"]
        return cached
    domain, answered = await _suggested(name)
    if not domain:
        wiki, wiki_answered = await _wikidata(name)
        domain, answered = wiki, answered or wiki_answered
    if domain or answered:
        cache[key] = {"domain": domain, "at": now}
        json_cache_put(cache_dir("domains.json"), cache)
    return domain
