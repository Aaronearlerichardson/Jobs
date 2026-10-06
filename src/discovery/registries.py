"""Structured name sources: public registries that list a state's employers.

A directory page needs scraping and a web search needs luck; a registry
answers with the state's companies in one query. `discover_registries` reads
the `[discovery].registries` that are enabled, drops every name the roster
already holds, and resolves the rest a batch at a time (a cursor persisted
under `.cache/`), queueing boards for review as `registry:<name>` and
recording the rest as misses.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import date
from typing import Any, NamedTuple

from src import config, store
from src.match.names import junk_name_reason, name_key, strip_suffixes
from src.net import http
from src.net.util import cache_dir, json_cache_get, json_cache_put
from src.rows import BoardHit
from .local_sourcing import queue_names
from .name_sources import blocked_keys


class NamedSource(NamedTuple):
    """An employer a registry names; `website` and `city` when it says."""
    name: str
    website: str | None
    city: str | None
    source: str


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


def _named(rows: Iterable[tuple[str | None, str | None]], label: str) -> list[NamedSource]:
    """One source per distinct, employer-shaped cleaned name of the
    (name, city) `rows`, in order of first appearance."""
    out: dict[str, NamedSource] = {}
    for raw, city in rows:
        name = _clean(raw)
        if name and not junk_name_reason(name):
            out.setdefault(name_key(name), NamedSource(
                name, None, (city or "").title() or None, f"registry:{label}"))
    return list(out.values())


def _results(payload: Any) -> list[dict[str, Any]]:
    """The dict entries of a registry reply's `results`, [] when it has none."""
    results = payload.get("results") if isinstance(payload, dict) else None
    return [r for r in results or [] if isinstance(r, dict)]


def nih_rows(payload: Any) -> list[tuple[str | None, str | None]]:
    """(organization, city) of each project in a RePORTER reply.

    >>> nih_rows({"results": [{"organization": {"org_name": "A", "org_city": "B"}}]})
    [('A', 'B')]
    >>> nih_rows(None)
    []
    """
    orgs = [(r.get("organization") or {}) for r in _results(payload)]
    return [(o.get("org_name"), o.get("org_city")) for o in orgs]


def fda_rows(payload: Any) -> list[tuple[str | None, str | None]]:
    """(establishment, None) of each term in an openFDA count reply.

    >>> fda_rows({"results": [{"term": "Acme Medical LLC", "count": 3}]})
    [('Acme Medical LLC', None)]
    """
    return [(r.get("term"), None) for r in _results(payload)]


async def nih_sbir(state: str) -> list[NamedSource]:
    """Small businesses with an SBIR/STTR project in `state`, this fiscal
    year and last (NIH RePORTER)."""
    cfg = config.REGISTRIES["nih_sbir"]
    body = {"criteria": {"org_states": [state], "activity_codes": cfg["activity_codes"],
                         "fiscal_years": fiscal_years(date.today())},
            "include_fields": ["Organization"], "limit": cfg["page"]}
    rows: list[tuple[str | None, str | None]] = []
    offset = 0
    while offset <= cfg["max_offset"]:
        _status, data, err = await http.request_json(
            "POST", cfg["url"], "nih reporter", json={**body, "offset": offset})
        if err:
            break
        page = nih_rows(data)
        rows += page
        offset += cfg["page"]
        total = ((data.get("meta") or {}).get("total") or 0) if isinstance(data, dict) else 0
        if not page or offset >= total:
            break
    return _named(rows, "nih_sbir")


async def openfda_devices(state: str) -> list[NamedSource]:
    """Medical-device establishments registered in `state` (openFDA)."""
    cfg = config.REGISTRIES["openfda_devices"]
    data = await http.get_json(
        cfg["url"], "openfda", params={
            "search": f"registration.state_code:{state}",
            "count": "registration.name.exact", "limit": cfg["limit"]})
    return _named(fda_rows(data), "openfda_devices")


READERS: dict[str, Callable[[str], Awaitable[list[NamedSource]]]] = {
    "nih_sbir": nih_sbir, "openfda_devices": openfda_devices}


def next_batch(keys: Sequence[str], cursor: str, limit: int) -> list[str]:
    """Up to `limit` of the sorted `keys` after `cursor`, wrapping to the
    start when the end is reached.

    >>> keys = ["a", "b", "c", "d"]
    >>> next_batch(keys, "", 2), next_batch(keys, "b", 2), next_batch(keys, "c", 3)
    (['a', 'b'], ['c', 'd'], ['d', 'a', 'b'])
    """
    after = [k for k in keys if k > cursor]
    return (after + [k for k in keys if k <= cursor])[:limit]


def _known_keys(conn: sqlite3.Connection) -> set[str]:
    """Every name key the roster holds, misses included, bare and
    suffix-stripped (registry names arrive stripped), and every blocked one."""
    names = [r[0] for r in conn.execute("SELECT name FROM companies")]
    return ({name_key(s) for n in names for s in (n, strip_suffixes(n))}
            | blocked_keys(conn))


async def discover_registries(apply: bool = False, limit: int = 60) -> dict[str, int]:
    """Names from the enabled registries that the roster lacks: a dry run
    counts them per source; `apply` resolves the next `limit` of them from
    the cursor, queues boards for review and records the misses. Returns
    the counts."""
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
    for reader in readers:
        found = await READERS[reader](state)
        new = [s for s in found if name_key(s.name) not in known]
        counts[reader] = len(found)
        counts[f"{reader}_new"] = len(new)
        print(f"  {reader:16} {len(found):5} name(s), {len(found) - len(new)} on the "
              f"roster, {len(new)} new")
        for s in new:
            gathered.setdefault(name_key(s.name), s)
    path = cache_dir("registries_cursor.json")
    cursor = (json_cache_get(path, float("inf")) or {}).get("key", "")
    batch = next_batch(sorted(gathered), cursor, limit)
    counts |= {"new": len(gathered), "batch": len(batch), "queued": 0, "missed": 0}
    print(f"  {len(gathered)} new name(s) in all; the next batch is {len(batch)}")
    if not apply:
        print("  dry run: nothing written (--apply to resolve and queue the batch)")
        return counts
    queued: list[BoardHit] = []
    missed: list[tuple[str, str]] = []
    async with store.Writer() as db:
        for source in sorted({gathered[k].source for k in batch}):
            todo = [gathered[k] for k in batch if gathered[k].source == source]
            q, m = await queue_names(db, [s.name for s in todo], source,
                                     {s.name: s.website for s in todo if s.website})
            queued += q
            missed += m
    if batch:
        json_cache_put(path, {"key": batch[-1]})
    counts |= {"queued": len(queued), "missed": len(missed)}
    print(f"  resolved {len(queued)} of {len(batch)} ({len(missed)} missed); "
          f"{len(queued)} board(s) queued for review")
    return counts
