"""Detail hydration: the per-posting GETs that fill a listing's missing
body or location, within each host's tolerances. Shared by the harvest
pull (harvest_board's `hydrate`) and triage's hydrate phase."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from src import config
from src.ats.board import company as company_fetch
from src.net import http
from src.rows import CompanyRow, FetchedJob

_log = logging.getLogger(__name__)

# Hydration is a detail GET per posting, config.HYDRATE_DELAY_S apart and
# at most config.HYDRATE_CAP_PER_RUN per board per run: one host cut the
# crawler off after 151 detail GETs at two per second (2026-09-10), and
# again after 42 on a retry ten minutes later. The rows left bodiless are
# picked up by later runs (stored bodies are never re-fetched, so each run
# advances). A host that stops answering (hydrate_rows' miss streak) gets
# one pause this long, then the next run.
MISS_BACKOFF_S = 90.0


class BoardStats(http.SnapshotFields, total=False):
    """One board's harvest stats: the counters plus http.snapshot_info()'s keys."""
    name: str | None
    ats: str | None
    fetched: int
    new: int
    hydrated: int
    unhydrated: int
    closed: int
    reopened: int
    err: str | None
    secs: float
    tried: list[str]


async def hydrate_rows(jobs: list[FetchedJob], company: CompanyRow,
                       stats: BoardStats, delay: float | None = None,
                       backoff_s: float = MISS_BACKOFF_S,
                       progress: Callable[[], object] = lambda: None) -> list[str]:
    """Resolve every row in `jobs` that still needs a detail call
    (company_fetch.needs_detail: no body yet, or a body already but a
    location the listing never resolved), in place, within the host's
    tolerances: config.HYDRATE_CAP_PER_RUN rows, `delay` between GETs
    (config.HYDRATE_DELAY_S when None), and the miss-streak breaker;
    `progress()` after each GET and after the pause.

    Fills stats['hydrated'] (rows whose detail need was resolved this
    pass -- a body arrived, or a location-only row's location did) and
    stats['unhydrated'] (rows that still need one when this pass ends,
    whether never reached or tried and failed). A location lookup that
    comes back empty counts as a miss for the breaker exactly like a
    failed body fetch -- needs_detail decides "resolved or not" either
    way, so the two cases share one counter.

    Returns the ids of the rows it attempted; a row over the cap is not one
    (tests/test_triage.py::test_waiting_reason_tells_a_failed_fetch_from_a_row_over_the_cap).
    """
    # Consecutive misses that mean the host has stopped answering (some
    # drop the connection outright once they decide you are a bot). The
    # first streak earns one pause-and-retry; a second ends hydration for
    # this board, and the next run picks the bodiless rows up again.
    miss_streak = 5
    todo = await asyncio.to_thread(
        lambda: [j for j in jobs if company_fetch.needs_detail(j)])
    cap = config.HYDRATE_CAP_PER_RUN
    delay = config.HYDRATE_DELAY_S if delay is None else delay
    if len(todo) > cap:
        print(f"    {company.get('name')}: {len(todo)} row(s) needing detail, "
              f"cap is {cap}/run - the rest next run")
        todo = todo[:cap]
    streak = paused = 0
    tried: list[str] = []
    for i, j in enumerate(todo):
        _log.debug("hydrate %s", j.get("url"))
        tried.append(j["id"])
        try:
            await company_fetch.hydrate_description(j, company)
        except Exception as e:                  # noqa: BLE001 - per row
            _log.debug("hydrate %s failed: %s", j.get("url"), e)
        progress()
        if not company_fetch.needs_detail(j):
            stats["hydrated"] += 1
            streak = 0
        else:
            streak += 1
        if streak >= miss_streak:
            if paused:
                print(f"    [!] {company.get('name')}: {streak} more "
                      f"misses after a pause - {len(todo) - i - 1} row(s) left "
                      f"unresolved for the next run")
                break
            paused += 1
            print(f"    [!] {company.get('name')}: {streak} hydration "
                  f"misses in a row - pausing {backoff_s:.0f}s")
            streak = 0
            await asyncio.sleep(backoff_s)
            progress()
        elif delay:
            await asyncio.sleep(delay)
    stats["unhydrated"] = await asyncio.to_thread(
        lambda: sum(1 for j in jobs if company_fetch.needs_detail(j)))
    return tried
