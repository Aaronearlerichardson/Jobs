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

from src import store
from src.ats.feeds.careeronestop import fetch_nlx_company
from src.config import RuntimeTrack
from src.discovery import discover, print_summary, write_discovery_report
from src.discovery.dork import run_ddgs_dorks
from src.ops.ingest import ingest_external_jobs
from src.ops.maintenance import track_store, track_writer
from src.ops.repair import prune_dead_boards

if TYPE_CHECKING:
    from src.discovery.pipeline import DiscoveryResult


def dedup(t: RuntimeTrack | None = None) -> tuple[int, int]:
    """Merge duplicate company rows pointing at one board, then duplicate
    job rows (store.merge_jobs), then flag the cross-board duplicates of one employer
    (store.flag_duplicate_jobs). Returns (companies merged, job rows merged away)."""
    with track_store(t) as conn:
        n = store.dedup_companies(conn)
        n_jobs = store.dedup_jobs(conn)
        flagged = store.flag_duplicate_jobs(conn)
    print(f"\n  merged {n} duplicate company row(s) into their canonical board; "
          f"merged {n_jobs} duplicate job row(s); "
          f"{flagged} cross-board duplicate flag(s) changed.")
    return n, n_jobs


async def prune(offmission: bool = False, t: RuntimeTrack | None = None) -> tuple[int, int]:
    """Deactivate companies whose ATS board is dead, and optionally the
    off-mission ones. Returns (dead deactivated, off-mission deactivated)."""
    async with track_writer(t) as db:
        n_dead, n_off = await prune_dead_boards(
            db, deactivate_offmission=offmission)
    print(f"\n  deactivated {n_dead} dead-board compan(ies)"
          + (f" + {n_off} off-mission" if offmission else "") + ".")
    return n_dead, n_off


async def ingest_nlx(companies: list[str] | None, t: RuntimeTrack | None = None) -> int:
    """Pull postings for bot-gated employers from the federal NLx feed and
    run them through the standard ingest. `companies` is a list of
    employer names. Returns the number of new jobs ingested."""
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
    added, checked = await run_ddgs_dorks()
    print(f"\n  {added} new local board(s) added to the store "
          f"({checked} extracted from dork results)")
    return added, checked


async def discover_term(term: str | None, no_report: bool = False,
                        dry_run: bool = False) -> DiscoveryResult | None:
    """Free-text sector discovery (src.discovery.pipeline.discover): ask
    Claude for likely employers matching `term`, resolve each, and queue the
    boards for review unless `dry_run`. Returns the discovery result, or
    None when no term was given."""
    term = (term or "").strip()
    if not term:
        print("  [!] give a sector/term to search for, e.g. 'medical device companies'")
        return None
    result = await discover(term, dry_run=dry_run)
    print_summary(result)
    if not no_report:
        write_discovery_report(result)
    if result["queued"] and not dry_run:
        print("  Confirm the [review] rows in the web UI's Review section "
              "before they are crawled")
    return result
