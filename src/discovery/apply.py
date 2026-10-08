"""Employer attribution: aggregator postings -> roster rows, queued for review.

New rows land in the REVIEW QUEUE (src.store.mark_pending): a name an
aggregator posting carries is exactly the kind of name that used to reach
the roster without ever having been an employer.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING

from src import store
from src.ats import coords
from src.ats.registry import seed_tag_for
from src.ats.signatures import detect, pack
from src.match.names import name_key
from src.rows import CompanyIn, FetchedJob

if TYPE_CHECKING:
    import sqlite3


# --------------------------------------------------------------------------- #
#  Employer attribution: aggregator postings -> roster rows                    #
# --------------------------------------------------------------------------- #
#
# An aggregator board (a VC portfolio, an industry association) carries the
# openings of many employers and names the employer on each posting. The
# fetcher that reads such a board stamps `_employer` on every job; this is
# what happens next, and it is the same act as the rest of this module --
# turn something discovery found into a roster row, and queue it for
# review rather than trusting it.
#
# It lived in src/ats/feeds/getro.py, next to the parser that produces
# `_employer`. That put a store WRITE inside a fetcher: src/ats otherwise
# knows nothing about the roster, and this single function was the whole
# reason the package reached src/store at all. Nothing in it is
# Getro-specific except the `source` prefix, which is now an argument.


def _coords_from_urls(urls: Iterable[str | None]) -> CompanyIn | None:
    """Roster-shaped board coordinates for the employer, read off its
    apply links, or None when none of them names a known ATS."""
    for url in urls:
        hit = detect("", url or "", leads=False)
        if not hit:
            continue
        return coords.columns(hit[1], hit[2],
                              pack(hit[1], hit[2], url or "")["careers_url"])
    return None


def attribute_employers(conn: sqlite3.Connection, jobs: list[FetchedJob],
                        commit: bool = True, source: str = "getro") -> list[FetchedJob]:
    """Link each board-sourced job to its employer's roster row, queueing
    employers the roster lacks for review. Returns the jobs to keep.

    Jobs without an ``_employer`` record pass through untouched. For the
    rest, per employer:

    * a roster row that owns the same board (the apply link's ATS
      coordinates — ``src.store.company_by_board``) or the same name gets
      ``company_id`` stamped on the jobs. When that row is ACTIVE and
      confirmed, its own crawl covers the board, so a posting the store
      already holds under the employer's URL is dropped here rather than
      stored twice;
    * a name the reviewer rejected (``src.store.block_name``) drops its
      jobs — that decision was "not a company", and it sticks;
    * anything else becomes a review candidate under `commit`:
      ``src.store.mark_pending`` (inactive, review pending), with
      ``source = "getro:<board host>"`` and the ATS coordinates when the
      apply link revealed them. Never an active row.

    `source` names the aggregator in the stored `source` column
    ("getro:<board host>"). Getro is the only fetcher stamping
    `_employer` today; the argument is here so the next one does not
    have to fork this.

    See tests/test_fetcher_parsers.py::TestGetroAttribution.
    """
    groups: dict[str, list[FetchedJob]] = {}
    for j in jobs:
        emp = j.get("_employer")
        if isinstance(emp, dict) and (emp.get("name") or emp.get("slug")):
            groups.setdefault(emp.get("slug") or emp["name"].lower(),
                              []).append(j)
    if not groups:
        return list(jobs)

    blocked = store.blocked_keys(conn)
    drop: set[int] = set()
    for key, group in groups.items():
        emp = group[0]["_employer"]
        name = emp.get("name") or key
        coords = _coords_from_urls([j.get("url") for j in group])
        row = store.company_by_board(conn, coords) if coords else None
        if row is None:
            cid = store.company_id_by_name(conn, name)
            row = store.get_company(conn, cid) if cid else None
        if row is not None:
            crawled = bool(row.get("active")) and row["review"] != "pending"
            urls: list[str] = [j["url"] for j in group if j.get("url")] if crawled else []
            stored = {r[0] for r in conn.execute(
                "SELECT url FROM jobs WHERE url IN (SELECT value FROM json_each(?))",
                (json.dumps(urls),))} if urls else set()
            for j in group:
                j["company_id"] = row["id"]
                if j.get("url") in stored:
                    drop.add(id(j))
            continue
        if name_key(name) in blocked:
            drop.update(id(j) for j in group)
            continue
        if not commit:
            continue
        via = f"{source}:{emp.get('board') or ''}"
        careers_url = ((coords or {}).get("careers_url")
                       or (f"https://{emp['domain']}" if emp.get("domain")
                           else None))
        candidate: CompanyIn = {"name": name, "careers_url": careers_url,
                                 "source": via,
                                 "notes": f"employer on the {emp.get('board')} board; "
                                          f"{len(group)} relevant posting(s)"}
        if coords:
            candidate.update(coords)
            candidate["careers_url"] = careers_url
            candidate["tags"] = seed_tag_for(coords["ats"])
        cid = store.add_board(conn, store.mark_pending(candidate))[0]
        for j in group:
            j["company_id"] = cid
    return [j for j in jobs if id(j) not in drop]
