"""Structured name sources: public registries that list a state's employers.

A directory page needs scraping and a web search needs luck; a registry
answers with the state's companies in one query. `discover_registries` reads
the `[discovery].registries` that are enabled, drops every name the roster
already holds, ranks the rest by how much their project titles read like the
mission-aligned employers' (`vocab.title_vocab`), and resolves the
best unprocessed batch at a time, queueing boards for review as
`registry:<name>` and recording the rest as misses.

`google_jobs` is the paid one: employers hiring near you for your kind of
role, as Google Jobs lists them, read through an Apify actor.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import date
from collections.abc import Mapping
from typing import NamedTuple, cast

from pydantic import BaseModel

from src import config, store
from src.match.locality import is_nc
from src.match.names import junk_name_reason, name_key, strip_suffixes
from src.net import apify, http
from src.net.util import cache_dir, hashed_cache_path, json_cache_get, json_cache_put
from src.net.util import JSON
from src.rows import str_or_none
from .local_sourcing import queue_names
from .vocab import title_vocab, word_score, words


class NamedSource(NamedTuple):
    """An employer a registry names; `website` and `city` when it says, and
    a text `blurb` of what it does (project titles) for the mission ranking."""
    name: str
    website: str | None
    city: str | None
    source: str
    blurb: str | None = None


def state_code(suffixes: Iterable[str]) -> str | None:
    """The first two-letter entry of `[locality].state_suffix`, upper-cased.

    >>> state_code(["nc", "north carolina"]), state_code(["texas"])
    ('NC', None)
    """
    return next((s.upper() for s in suffixes if len(s) == 2 and s.isalpha()), None)


def fiscal_years(today: date) -> list[int]:
    """This US federal fiscal year (it starts 1 October) and the last.

    >>> fiscal_years(date(2026, 9, 30)), fiscal_years(date(2026, 10, 1))
    ([2025, 2026], [2026, 2027])
    """
    this = today.year + (today.month >= 10)
    return [this - 1, this]


def _clean(name: str | None) -> str:
    """A registry's all-caps name title-cased, less its suffix words.

    >>> _clean("PERSISTENCE THERAPEUTICS, INC."), _clean("Teleflex Medical LLC")
    ('Persistence', 'Teleflex Medical')
    """
    n = (name or "").strip()
    return strip_suffixes(n.title() if n.isupper() else n)


def _display(name: str | None) -> str:
    """A job listing's employer name less a trailing legal form. Its
    industry words stay (`_clean` drops them): a listing names the
    employer as it calls itself, not by its registered legal name.

    >>> _display("Ten63 Therapeutics, Inc."), _display("CVS Health"), _display(None)
    ('Ten63 Therapeutics', 'CVS Health', '')
    """
    return re.sub(r"[,\s]+(?:inc|incorporated|llc|l\.l\.c|ltd|corp|corporation|co|plc|gmbh|lp|llp)"
                  r"\.?$", "", (name or "").strip(), flags=re.IGNORECASE)


def _named(rows: Iterable[tuple[str | None, str | None, str | None]], label: str,
           clean: Callable[[str | None], str] = _clean) -> list[NamedSource]:
    """One source per distinct, employer-shaped `clean` name of the
    (name, city, text) `rows`, in order of first appearance, its distinct
    texts joined as the blurb.

    >>> [(s.name, s.blurb) for s in _named([("ACME INC", None, "x"), ("Acme", None, "y"),
    ...                                     ("Acme", None, "x"), ("Beta", None, None)], "t")]
    [('Acme', 'x; y'), ('Beta', None)]
    """
    out: dict[str, NamedSource] = {}
    for raw, city, text in rows:
        name = clean(raw)
        if not name or junk_name_reason(name):
            continue
        key = name_key(name)
        prev = out.get(key) or NamedSource(
            name, None, (city or "").title() or None, f"registry:{label}")
        texts = prev.blurb.split("; ") if prev.blurb else []
        if text and text not in texts:
            texts.append(text)
        out[key] = prev._replace(blurb="; ".join(texts) or None)
    return list(out.values())


def _results(payload: JSON) -> list[Mapping[str, JSON]]:
    """The dict entries of a registry reply's `results`, [] when it has none."""
    results = payload.get("results") if isinstance(payload, Mapping) else None
    return [r for r in results or [] if isinstance(r, Mapping)] if isinstance(results, list) else []


def nih_rows(payload: JSON) -> list[tuple[str | None, str | None, str | None]]:
    """(organization, city, project title) of each project in a RePORTER reply.

    >>> nih_rows({"results": [{"project_title": "T",
    ...                        "organization": {"org_name": "A", "org_city": "B"}}]})
    [('A', 'B', 'T')]
    >>> nih_rows(None)
    []
    """
    field = config.REGISTRIES["nih_sbir"]["blurb_field"]
    rows: list[tuple[str | None, str | None, str | None]] = []
    for r in _results(payload):
        org = r.get("organization")
        o: Mapping[str, JSON] = org if isinstance(org, Mapping) else {}
        rows.append((str_or_none(o.get("org_name")), str_or_none(o.get("org_city")), str_or_none(r.get(field))))
    return rows


def fda_rows(payload: JSON) -> list[tuple[str | None, str | None, str | None]]:
    """(establishment, None, None) of each term in an openFDA count reply.

    >>> fda_rows({"results": [{"term": "Acme Medical LLC", "count": 3}]})
    [('Acme Medical LLC', None, None)]
    """
    return [(str_or_none(r.get("term")), None, None) for r in _results(payload)]


def fda_search(state: str, specialties: Sequence[str]) -> str:
    """The openFDA query for `state`'s establishments, narrowed to devices in
    any of `specialties` when there are some.

    >>> fda_search("NC", [])
    'registration.state_code:NC'
    >>> fda_search("NC", ["Neurology", 'Ear "x"'])  # doctest: +ELLIPSIS
    'registration.state_code:NC AND (...exact:"Neurology" OR ...exact:"Ear  x ")'
    """
    cfg = config.REGISTRIES["openfda_devices"]
    clauses = [cfg["specialty_search"].format(value=v.replace('"', " ")) for v in specialties]
    base = cfg["search"].format(state=state)
    return f"{base} AND ({' OR '.join(clauses)})" if clauses else base


class _Meta(BaseModel):
    total: int | None = None


class _Reply(BaseModel):
    """The part of a RePORTER reply the pager reads."""
    meta: _Meta | None = None


async def nih_sbir(state: str) -> list[NamedSource]:
    """Small businesses with an SBIR/STTR project in `state`, this fiscal
    year and last (NIH RePORTER)."""
    cfg = config.REGISTRIES["nih_sbir"]
    body = {"criteria": {"org_states": [state], "activity_codes": cfg["activity_codes"],
                         "fiscal_years": fiscal_years(date.today())},
            "include_fields": cfg["include_fields"], "limit": cfg["page"]}
    rows: list[tuple[str | None, str | None, str | None]] = []
    offset = 0
    while offset <= cfg["max_offset"]:
        _status, data, err = await http.request_json(
            "POST", cfg["url"], "nih reporter", json={**body, "offset": offset})
        if err:
            break
        page = nih_rows(data)
        rows += page
        offset += cfg["page"]
        meta = _Reply.model_validate(data if isinstance(data, Mapping) else {}).meta
        total = meta.total if meta else None
        if not page or offset >= (total or 0):
            break
    return _named(rows, "nih_sbir")


async def openfda_devices(state: str) -> list[NamedSource]:
    """Medical-device establishments registered in `state` (openFDA), those
    with a device in `[discovery].registry_specialties` when it is set."""
    cfg = config.REGISTRIES["openfda_devices"]
    data = await http.get_json(
        cfg["url"], "openfda", params={
            "search": fda_search(state, config.DISCOVERY_REGISTRY_SPECIALTIES),
            "count": cfg["count"], "limit": cfg["limit"]})
    return _named(fda_rows(data), "openfda_devices")


def fill(template: Mapping[str, JSON], values: Mapping[str, JSON]) -> dict[str, JSON]:
    """`template` with each string value that is exactly "{name}", for a
    name in `values`, replaced by that value.

    >>> fill({"q": "{searches}", "n": "{max_items}", "c": "us", "x": "{other}"},
    ...      {"searches": ["a b"], "max_items": 5})
    {'q': ['a b'], 'n': 5, 'c': 'us', 'x': '{other}'}
    """
    return {k: values.get(v[1:-1], v) if isinstance(v, str) and v[:1] == "{" and v[-1:] == "}"
            else v for k, v in template.items()}


def places(locations: Sequence[str], substrings: Sequence[str], state_suffix: Sequence[str]
           ) -> list[str]:
    """`locations` when given, else "<Place>, <ST>" for each [locality]
    place `substrings` names that is not the state itself.

    >>> places([], ["north carolina", "durham", "chapel hill"], ["nc", "north carolina"])
    ['Durham, NC', 'Chapel Hill, NC']
    >>> places(["Austin, TX"], ["durham"], ["nc"]), places([], ["durham"], ["texas"])
    (['Austin, TX'], [])
    """
    if locations:
        return list(locations)
    state = state_code(state_suffix)
    states = {s.lower() for s in state_suffix}
    return [f"{p.title()}, {state}" for p in substrings if p.lower() not in states] if state else []


def searches(queries: Sequence[str], where: Sequence[str], cap: int) -> list[str]:
    """"<query> <place>" for every query in the first place, then the
    second, and so on: at most `cap`.

    >>> searches(["data scientist", "bioinformatics"], ["Durham, NC", "Cary, NC"], 3)
    ['data scientist Durham, NC', 'bioinformatics Durham, NC', 'data scientist Cary, NC']
    """
    return [f"{q} {p}" for p in where for q in queries][:cap]


def posting_rows(items: Iterable[Mapping[str, JSON]], keys: Mapping[str, Sequence[str]],
                 local: Callable[[str | None], bool] = is_nc
                 ) -> list[tuple[str | None, str | None, str | None]]:
    """(employer, city, title) of each posting whose location is `local`
    (in your [locality]), each read from the first of its `keys`
    ("company", "location", "title" lists) the item has.

    >>> keys = {"company": ["company_name", "company"], "location": ["location"],
    ...         "title": ["job_title"]}
    >>> posting_rows([{"company": "Acme Bio", "location": "Here, ST", "job_title": "ML"},
    ...               {"company_name": "Far Co", "location": "Anywhere", "job_title": "ML"}],
    ...              keys, local=lambda where: where == "Here, ST")
    [('Acme Bio', 'Here', 'ML')]
    """
    def first(item: Mapping[str, JSON], field: str) -> str | None:
        return next((v for k in keys[field] if (v := str_or_none(item.get(k)))), None)
    rows: list[tuple[str | None, str | None, str | None]] = []
    for item in items:
        where = first(item, "location")
        if local(where):
            rows.append((first(item, "company"), (where or "").split(",")[0].strip(),
                         first(item, "title")))
    return rows


async def google_jobs(state: str) -> list[NamedSource]:
    """Employers with a posting in your [locality] that Google Jobs lists
    for the [sources.google_jobs] searches, their job titles as the blurb:
    the `google_jobs` spec's Apify actor, run at most once per
    `refresh_days` (a dry run and the --apply after it share one run)."""
    cfg, spec = config.GOOGLE_JOBS, config.REGISTRIES["google_jobs"]
    track = config.track_for_engine("local")
    kw = config.KEYWORDS_BY_TRACK.get(track.id)
    queries = cfg.queries or (kw.core if kw else [])
    where = places(cfg.locations, config.LOCALITY_SUBSTRINGS, config.LOCALITY_STATE_SUFFIX)
    wanted = searches(queries, where, cfg.max_searches)
    if not wanted:
        print("  google_jobs skipped: needs [sources.google_jobs] queries and locations "
              "(or a location-scoped track's core keywords and [locality] places)")
        return []
    run_input = fill(spec["input"], {"searches": wanted, "queries": list(queries),
                                     "locations": where, "max_items": cfg.max_items})
    path = hashed_cache_path(cache_dir("google_jobs"),
                             json.dumps([spec["actor"], run_input], sort_keys=True))
    items = cast(list[Mapping[str, JSON]] | None,
                 await asyncio.to_thread(json_cache_get, path, cfg.refresh_days * 86400))
    if items is None:
        print(f"  google_jobs: {len(wanted)} search(es), at most {cfg.max_items} postings "
              f"and ${cfg.max_charge_usd:.2f}")
        items = await apify.run_actor(spec["actor"], run_input, max_charge_usd=cfg.max_charge_usd)
        if items is None:
            return []
        await asyncio.to_thread(json_cache_put, path, items)
    keys = {"company": spec["company_keys"], "location": spec["location_keys"],
            "title": spec["title_keys"]}
    # An employer field that names one of your places ("nc") names no employer.
    here = {name_key(p) for p in (*config.LOCALITY_SUBSTRINGS, *config.LOCALITY_WORD_TOKENS,
                                  *config.LOCALITY_STATE_SUFFIX)}
    rows = [r for r in posting_rows(items, keys) if name_key(r[0]) not in here]
    return _named(rows, "google_jobs", clean=_display)


READERS: dict[str, Callable[[str], Awaitable[list[NamedSource]]]] = {
    "nih_sbir": nih_sbir, "openfda_devices": openfda_devices, "google_jobs": google_jobs}


def ranked(sources: Iterable[NamedSource], vocab: dict[str, float]) -> list[NamedSource]:
    """`sources`, best mission fit first: the `word_score` of the blurb's
    words. No blurb (or no `vocab`) scores zero; ties go in name order.

    >>> a, b, c = (NamedSource(n, None, None, "r", t) for n, t in
    ...            [("A", "store"), ("B", None), ("C", "clinical")])
    >>> [s.name for s in ranked([a, b, c], {"store": -1.0, "clinical": 1.0})]
    ['C', 'B', 'A']
    >>> [s.name for s in ranked([c, b, a], {})]
    ['A', 'B', 'C']
    """
    def fit(s: NamedSource) -> float:
        return word_score(words(s.blurb), vocab) if s.blurb and vocab else 0.0
    return sorted(sources, key=lambda s: (-fit(s), name_key(s.name)))


def _known_keys(conn: sqlite3.Connection) -> set[str]:
    """Every name key the roster holds, misses included, bare and
    suffix-stripped (registry names arrive stripped), and every blocked one."""
    names = [r[0] for r in conn.execute("SELECT name FROM companies")]
    return ({name_key(s) for n in names for s in (n, strip_suffixes(n))}
            | store.blocked_keys(conn))


async def discover_registries(apply: bool = False, limit: int = 60) -> dict[str, int]:
    """Names from the enabled registries that the roster lacks, best mission
    fit first (`ranked`): a dry run counts them per source; `apply` resolves
    the best `limit` not yet processed, queues boards for review and records
    the misses. Returns the counts.

    Notes:
        Processed keys are kept in `.cache/registries_done.json` because a
        name whose board the roster holds under another name writes no row,
        and would otherwise be re-resolved every run.
    """
    state = state_code(config.LOCALITY_STATE_SUFFIX)
    readers = [r for r in config.DISCOVERY_REGISTRIES if r in READERS]
    if not (state and readers):
        print("  registries skipped: needs [discovery].registries and a two-letter "
              "[locality].state_suffix entry")
        return {}
    gathered: dict[str, NamedSource] = {}
    counts: dict[str, int] = {}
    async with store.Writer() as db:
        known = await db.run(_known_keys)
        vocab = await db.run(title_vocab)
    # One reader per host, so they read side by side; results keep reader order.
    results = await asyncio.gather(*(READERS[r](state) for r in readers))
    for reader, found in zip(readers, results):
        new = [s for s in found if name_key(s.name) not in known]
        counts[reader] = len(found)
        counts[f"{reader}_new"] = len(new)
        print(f"  {reader:16} {len(found):5} name(s), {len(found) - len(new)} on the "
              f"roster, {len(new)} new")
        for s in new:
            if not (prev := gathered.get(name_key(s.name))) or (s.blurb and not prev.blurb):
                gathered[name_key(s.name)] = s
    path = cache_dir("registries_done.json")
    done = set((cast(dict[str, list[str]], json_cache_get(path, float("inf"))) or {}).get("done", []))
    todo = [s for s in ranked(gathered.values(), vocab) if name_key(s.name) not in done]
    batch = [name_key(s.name) for s in todo[:limit]]
    counts |= {"new": len(gathered), "batch": len(batch), "queued": 0, "missed": 0}
    print(f"  {len(gathered)} new name(s) in all, {len(todo)} not yet processed; "
          f"the next batch is {len(batch)}: {', '.join(s.name for s in todo[:5])}"
          f"{' ...' * (len(batch) > 5)}")
    if not apply:
        print("  dry run: nothing written (--apply to resolve and queue the batch)")
        return counts
    part = [gathered[k] for k in batch]
    async with store.Writer() as db:
        queued, missed = await queue_names(db, {s.name: s.source for s in part},
                                           {s.name: s.website for s in part if s.website})
    if batch:
        json_cache_put(path, {"done": sorted(done | set(batch))})
    counts |= {"queued": len(queued), "missed": len(missed)}
    print(f"  resolved {len(queued)} of {len(batch)} ({len(missed)} missed); "
          f"{len(queued)} board(s) queued for review")
    return counts
