"""Composite operation targets for src/dispatch/registry.py.

Each function here is the glue that used to be spelled out inline in one
front end (open the track's store, call a maintenance function, close and
report) and re-spelled slightly differently in another. Anything that is
a single existing function is targeted directly by the registry; only
multi-step operations live here.

The store is opened through maintenance.track_store, the same helper the
maintenance ops use. These three used to open it themselves, and resolved
`t=None` to the default DB FILE where maintenance resolves it to the
default TRACK -- so under a profile that gives a track its own `db`, one
op reached a different store depending on which front end asked for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.config import RuntimeTrack

if TYPE_CHECKING:
    from src.discovery.pipeline import DiscoveryResult


def dedup(t: RuntimeTrack | None = None) -> tuple[int, int]:
    """Merge duplicate company rows pointing at one board, then duplicate
    job rows, then flag the cross-board duplicates of one employer
    (store.flag_duplicate_jobs). Returns (companies merged, jobs dropped)."""
    from src import store
    from src.ops.maintenance import track_store
    with track_store(t) as conn:
        n = store.dedup_companies(conn)
        n_jobs = store.dedup_jobs(conn)
        flagged = store.flag_duplicate_jobs(conn)
    print(f"\n  merged {n} duplicate company row(s) into their canonical board; "
          f"dropped {n_jobs} duplicate job row(s); "
          f"{flagged} cross-board duplicate flag(s) changed.")
    return n, n_jobs


async def prune(offmission: bool = False, t: RuntimeTrack | None = None) -> tuple[int, int]:
    """Deactivate companies whose ATS board is dead, and optionally the
    off-mission ones. Returns (dead deactivated, off-mission deactivated)."""
    from src.ops.maintenance import track_writer
    from src.ops.repair import prune_dead_boards
    async with track_writer(t) as db:
        n_dead, n_off = await prune_dead_boards(
            db, deactivate_offmission=bool(offmission))
    print(f"\n  deactivated {n_dead} dead-board compan(ies)"
          + (f" + {n_off} off-mission" if offmission else "") + ".")
    return n_dead, n_off


def backfill_axes(t: RuntimeTrack | None = None) -> int:
    """Populate the per-axis fit columns from fit_reason (offline)."""
    from src import store
    from src.ops.maintenance import track_store
    with track_store(t) as conn:
        return store.backfill_axis_columns(conn)


async def ingest_nlx(companies: list[str] | None, t: RuntimeTrack | None = None) -> int:
    """Pull postings for bot-gated employers from the federal NLx feed and
    run them through the standard ingest. `companies` is a list of
    employer names. Returns the number of new jobs ingested."""
    from src.ats.feeds.careeronestop import fetch_nlx_company
    from src.ops.ingest import ingest_external_jobs
    if not companies:
        print("  [!] give a comma-separated list of employer names")
        return 0
    total = 0
    for name in companies:
        jobs = await fetch_nlx_company(name)
        print(f"  {name}: {len(jobs)} NLx posting(s)")
        if jobs:
            total += await ingest_external_jobs(jobs, source="nlx", t=t)
    print(f"\n  {total} new job(s) ingested from the NLx feed.")
    return total


async def dork_sweep() -> tuple[int, int]:
    """ATS dorking via DuckDuckGo: mine search-indexed board URLs for
    companies in your locality into the store. Returns (added, checked)."""
    from src.discovery.dork import run_ddgs_dorks
    added, checked = await run_ddgs_dorks()
    print(f"\n  {added} new local board(s) added to the store "
          f"({checked} extracted from dork results)")
    return added, checked


async def discover_term(term: str | None, no_report: bool = False,
                        dry_run: bool = False) -> DiscoveryResult | None:
    """Free-text sector discovery: ask Claude for likely employers matching
    `term`, probe each against the ATS registry, and (apply-by-default)
    queue the confirmed ones unless `dry_run`. Returns the discovery
    result, or None when no term was given."""
    from src.discovery import apply_to_store, discover, print_summary, write_discovery_report
    term = (term or "").strip()
    if not term:
        print("  [!] give a sector/term to search for, e.g. 'medical device companies'")
        return None
    result = await discover(term)
    print_summary(result)
    if not no_report:
        write_discovery_report(result)
    for line in await apply_to_store(result, dry_run=bool(dry_run)):
        print(line)
    return result
