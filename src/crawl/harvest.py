"""Background whole-board harvester: fetch EVERY board, store everything,
score nothing.

The daily crawl (src/crawl/runner.py) is deliberately narrow: it fetches only
the companies due for a crawl (active, not parked dormant), scoped to your
locality unless the board is watched, sweep-tagged or above the mission
floor, and it pays for description hydration only on rows that survive the
gates. That keeps a crawl to minutes, at the price of never seeing most of
what the roster's boards actually list.

This module is the other half: a slow, thorough pass that runs in the
background (Task Scheduler at log-on, repeating every few hours) and

  * takes every company with a fetchable board (store.harvestable_companies:
    active or not, dormant or not, any tag -- only rows with no board, or a
    blocklisted name, are skipped), except an inactive one mission-scored
    off-mission (plan), whose open postings the pass closes instead
    (store.retire_stopped);
  * pulls the WHOLE board, no location filter, as a bodiless listing;
  * stores the rows unscored and untracked (jobs.harvested_at stamped), and
    reconciles open/closed status against the full snapshot;
  * never touches crawl_state;
  * then hands everything pending to the triage pass (src/crawl/triage.py),
    which runs the crawl's gates cheapest-first, hydrates only the
    survivors, and fit-scores only the hydrated survivors.

Triage is where the Claude spend is: one mission call per never-scored
company and one fit call per posting that clears four free gates. A row
triage surfaces carries its track labels and score exactly as a crawled
row would; a row it drops records the gate that dropped it. The crawl
still adopts anything triage has not reached yet: a row without a track
label is "fresh" to it (store.crawl_seen), so it is gated and scored like
a posting seen for the first time, only without the network round-trips
(the runner reuses the stored description).

Concurrency: several processes write the one SQLite file (the web UI, the
scheduled crawl, this). store.connect runs WAL with a busy timeout, and
each board is written in ONE transaction (store.batch), so a 1,000-row
board takes the write lock once rather than a thousand times. Within the
pass, one thread owns the store connection (store.Writer) and the boards'
writes queue for it.

Wall clock is the only thing this trades away: the whole roster is on the
order of 20,000 postings, and even the listings alone take a while at
polite pacing (with --hydrate, every posting's detail GET on top of that
is hours). The pull (`pull`) is one coroutine: one task per host walks
that host's boards one at a time, largest first, and every host runs at
once, so a board's host never sees two of the pass's boards at a time.
Ctrl+C or the pass budget cancels it: committed boards stay, a board
still being written rolls back, and no queued board starts.
A board that makes no progress for pull's `stall_s` is cut off alone.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Collection, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from src import config
from src import store
from src.ats.board import board_for
from src.ats.board import company as company_fetch
from src.ats.coords import slug_named
from src.claude.api import api_disabled, have_api_key, report_cache_stats
from src.match.locality import geo_mode, location_unknown
from src.net import http
from src.net.util import worker_count
from src.rows import CompanyRow, JobIn
from src.ops.maintenance import rewrite_digest
from src.ops.scoring import verify_top
from src.ops.status import check_closed_jobs

_log = logging.getLogger(__name__)

# Workers of the pass's second half: triage's scoring and verify
# (triage's hydration and the closed-URL probe walk hosts, as the pull does).
DEFAULT_WORKERS = worker_count("harvest_workers")
# Hydration is a detail GET per posting, config.HYDRATE_DELAY_S apart and
# at most config.HYDRATE_CAP_PER_RUN per board per run: one host cut the
# crawler off after 151 detail GETs at two per second (2026-09-10), and
# again after 42 on a retry ten minutes later. The rows left bodiless are
# picked up by later runs (stored bodies are never re-fetched, so each run
# advances). A host that stops answering (hydrate_rows' miss streak) gets
# one pause this long, then the next run.
MISS_BACKOFF_S = 90.0
# The post-triage closed-URL probe (ops.check_closed_jobs): how stale a
# tracked OPEN row has to be (no board has vouched for it in this many
# days) before its detail URL is worth a live GET, and how many such probes
# one pass spends. A host takes at most config.CLOSED_PROBE_PER_HOST of
# them, paced, so this bounds the pass's total, not its time.
CLOSED_PROBE_STALE_DAYS = 7
CLOSED_PROBE_LIMIT = 500


# --------------------------------------------------------------------------- #
#  Planning                                                                    #
# --------------------------------------------------------------------------- #

def deferred_note(stats: dict[str, Any]) -> str:
    """The clause naming the boards a plan() `stats` dict left out as
    off-mission and inactive, deferred to the long interval or stopped;
    "" when there are none. run()'s header and harvest.py --list both
    print it; neither spells it out.

    >>> deferred_note({"offmission_skipped": 0, "offmission_stopped": 0})
    ''
    >>> stats = {"offmission_skipped": 2, "offmission_stopped": 347}
    >>> deferred_note(stats)    # doctest: +ELLIPSIS
    ', 2 off-mission board(s) deferred to ...h, 347 left out (inactive, scored off-mission)'
    """
    n, m = stats.get("offmission_skipped", 0), stats.get("offmission_stopped", 0)
    return ((f", {n} off-mission board(s) deferred to "
             f"{config.HARVEST_OFFMISSION_HOURS:g}h" if n else "")
            + (f", {m} left out (inactive, scored off-mission)" if m else ""))


def plan(conn: sqlite3.Connection, only: Collection[str] | None = None,
         names: Iterable[str] | None = None, min_age_hours: float | None = None,
         limit: int | None = None, now: datetime | None = None,
         stats: dict[str, Any] | None = None) -> list[CompanyRow]:
    """The boards this run will pull, in run order.

    `only` restricts to a set of ATS names, `names` to company names
    (case-insensitive); boards harvested within `min_age_hours` are skipped
    unless named explicitly. Largest boards first (their last known
    total_job_count): each host walks its boards in this order, and its
    longest walk is what the pass waits for.

    A board config.offmission_inactive calls "deferred" (inactive, never
    mission-scored) waits the longer config.HARVEST_OFFMISSION_HOURS
    instead of `min_age_hours`; one it calls "stopped" (inactive, scored
    into a tier the profile marks inactive) is left out whatever
    `min_age_hours` says, until it is reactivated or re-tiered. Naming a
    board explicitly bypasses both.

    `min_age_hours=None` -- the default, and what harvest.py passes when
    the flag is absent -- means the pass chooses both intervals itself:
    MIN_AGE_HOURS for every ordinary board, the long one for these. ANY
    explicit value is a caller override and applies uniformly, off-mission
    boards included, so `--min-age-hours 0` keeps meaning literally
    everything. The sentinel is what makes that honest: a plain
    `min_age_hours=6` is a deliberate "re-check everything that is 6h
    old", not an accidental repeat of the default, and is obeyed as one.

    `stats`, when given, gets `"offmission_skipped"` set to the number of
    boards this call left out ONLY because of the long interval -- i.e.
    boards that would already be due under plain `min_age_hours` freshness
    --, `"offmission_stopped"` to the number it left out as stopped, and
    `"min_age_hours"` to the ordinary cutoff this call resolved the
    sentinel to. All three are what a caller's header line says (pull
    below, harvest.py --list); neither re-derives the default itself, so
    the sentinel rule has exactly one writer. The out-parameter shape is
    this module's own (see hydrate_rows' `stats`): the return value is
    the plan, and a second return value would be read by no one who does
    not already hold the list.

    >>> conn = store.connect(":memory:")
    >>> for n, a, t, h in [("Big", "workday", 900, None),
    ...                    ("Small", "greenhouse", 12, None),
    ...                    ("Fresh", "lever", 5, "2026-09-10T08:00:00"),
    ...                    ("Old", "lever", 5, "2026-09-09T08:00:00")]:
    ...     _ = store.upsert_company(conn, {
    ...         "name": n, "ats": a, "slug": n.lower(), "total_job_count": t,
    ...         "wd_tenant": "big", "wd_pod": 5, "wd_site": "Ext"})
    ...     conn.execute("UPDATE companies SET last_harvested_at=? WHERE name=?",
    ...                  (h, n)).rowcount
    1
    1
    1
    1
    >>> now = datetime(2026, 9, 10, 9, 0)
    >>> [c["name"] for c in plan(conn, now=now)]
    ['Big', 'Small', 'Old']
    >>> [c["name"] for c in plan(conn, names=["fresh"], now=now)]
    ['Fresh']
    >>> [c["name"] for c in plan(conn, only={"workday"}, now=now)]
    ['Big']

    An inactive board never mission-scored waits the long interval instead
    of `min_age_hours` -- unless it is named explicitly, or the caller
    passes a `min_age_hours` other than the default:

    >>> _ = store.upsert_company(conn, {"name": "Stale", "ats": "lever",
    ...                                 "slug": "stale", "active": 0})
    >>> _ = conn.execute("UPDATE companies SET last_harvested_at=? "
    ...                  "WHERE name=?", ("2026-09-09T12:00:00", "Stale"))
    >>> stats = {}
    >>> [c["name"] for c in plan(conn, now=now, stats=stats)
    ...  if c["name"] == "Stale"]
    []
    >>> stats["offmission_skipped"]
    1
    >>> [c["name"] for c in plan(conn, min_age_hours=6, now=now)
    ...  if c["name"] == "Stale"]
    ['Stale']
    >>> [c["name"] for c in plan(conn, min_age_hours=0, now=now)
    ...  if c["name"] == "Stale"]
    ['Stale']
    >>> [c["name"] for c in plan(conn, names=["stale"], now=now)]
    ['Stale']

    One scored off-mission is left out at any `min_age_hours`, until it is
    reactivated:

    >>> _ = store.upsert_company(conn, {"name": "Parked", "ats": "lever", "slug": "p",
    ...                                 "active": 0, "mission_tier": "other"})
    >>> [c["name"] for c in plan(conn, min_age_hours=0, now=now, stats=stats)
    ...  if c["name"] == "Parked"], stats["offmission_stopped"]
    ([], 1)
    >>> _ = conn.execute("UPDATE companies SET active=1 WHERE name='Parked'")
    >>> "Parked" in [c["name"] for c in plan(conn, min_age_hours=0, now=now)]
    True

    Notes:
        The 2026-09-17 audit counted 289 off-mission inactive boards
        holding 51,134 stored jobs, against 288 active ones (any tier)
        holding 47,737 -- so roughly half of every whole-board pass was
        spent re-fetching rows triage's own mission gate discards on every
        single run.

        Until 2026-09-25 the cheap sweep platforms went first, then the
        smaller boards, for a thread pool that banked the most boards per
        minute; per-host walks finish soonest largest first.
    """
    now = now or datetime.now()
    # The sentinel, per the docstring: only an unset min_age_hours lets the
    # long interval apply at all.
    offmission_cutoff: str | None = None
    if min_age_hours is None:
        # A board harvested more recently than this is skipped, which is
        # what makes a restarted (or Task-Scheduler-repeated) run resume
        # where it left off. An inactive board never mission-scored waits
        # the longer config.HARVEST_OFFMISSION_HOURS instead -- see this
        # docstring.
        min_age_hours = 6.0
        offmission_cutoff = (
            now - timedelta(hours=config.HARVEST_OFFMISSION_HOURS)
        ).isoformat()
    cutoff = (now - timedelta(hours=min_age_hours)).isoformat()
    want = {n.strip().lower() for n in names or [] if n.strip()}
    rows: list[CompanyRow] = []
    offmission_skipped = offmission_stopped = 0
    for c in store.harvestable_companies(conn):
        if only and c.get("ats") not in only:
            continue
        if want:
            if (c.get("name") or "").lower() not in want:
                continue
        else:
            off = config.offmission_inactive(c)
            if off == "stopped":
                offmission_stopped += 1
                continue
            last = c.get("last_harvested_at") or ""
            board_cutoff = cutoff
            if offmission_cutoff is not None and off:
                board_cutoff = offmission_cutoff
                if cutoff >= last > offmission_cutoff:
                    offmission_skipped += 1
            if last > board_cutoff:
                continue
        rows.append(c)
    rows.sort(key=lambda c: (-(c.get("total_job_count") or 0),
                             (c.get("name") or "").lower()))
    if stats is not None:
        stats["offmission_skipped"] = offmission_skipped
        stats["offmission_stopped"] = offmission_stopped
        stats["min_age_hours"] = min_age_hours
    return rows[:limit] if limit else rows


# --------------------------------------------------------------------------- #
#  One board                                                                   #
# --------------------------------------------------------------------------- #

def _row(job: dict[str, Any], company: CompanyRow, stamp: str) -> JobIn:
    """The store row for one harvested posting: identity, body, dates -- no
    track, no score."""
    desc = (job.get("description") or "")[:config.MAX_DESC_CHARS]
    return {
        "job_id": job["id"], "company_id": company.get("id"),
        "company_name": company.get("name"), "title": job.get("title"),
        "url": job.get("url"), "location": job.get("location"),
        "geo_mode": geo_mode(job.get("location", ""), desc),
        "posted_at": job.get("posted_at"), "description": desc,
        "harvested_at": stamp,
    }


def _soft_failed(stats: dict[str, Any]) -> bool:
    """A board that answered with an error rather than an empty one: no
    rows, and its own fetch reported failures (net.http.snapshot_info).

    >>> _soft_failed({"fetched": 0, "fetch_errors": 2})
    True
    >>> _soft_failed({"fetched": 0, "fetch_errors": 0})
    False
    """
    return not stats.get("fetched") and bool(stats.get("fetch_errors"))


def bury_404_board(conn: sqlite3.Connection, company: CompanyRow,
                   error: str | None) -> str | None:
    """Mark `company` 'board-dead:<ats>' and deactivate it when `error`
    proves its board gone (`Board.gone`); the caller has already seen the
    board fail once before. Returns the reason written, else None.

    Deactivated exactly as mark_harvested's promotion is, which is what
    lets reresolve_misses re-check it."""
    ats = company.get("ats")
    board = board_for(ats)
    if not (board and board.gone(error)):
        return None
    reason = f"board-dead:{ats}"
    store.deactivate_company(conn, company["id"])
    store.record_miss(conn, company["name"], reason)
    print(f"    [!] {company['name']} ({ats}): listing endpoint 404 twice - "
          f"board-dead, deactivated (reresolve re-checks it)")
    return reason


async def harvest_board(company: CompanyRow, db: store.Writer, hydrate: bool = False,
                        delay: float | None = None, now: datetime | None = None,
                        backoff_s: float = MISS_BACKOFF_S,
                        progress: Callable[[], object] = lambda: None) -> dict[str, Any]:
    """Fetch, hydrate and store ONE board, its store work on `db` (the
    pass's store.Writer). Returns a stats dict; a fetch failure is
    reported, not raised, and leaves the store untouched (nothing is
    closed on a failed or empty snapshot; see _write_board for an
    incomplete or capped one). `delay` defaults to config.HYDRATE_DELAY_S.
    `progress()` is called when the listing is back and after every
    hydration GET (pull's stall bound).

    `hydrate` is OFF by default: the listing is stored bodiless and the
    triage pass (src/crawl/triage.py) fetches bodies for the rows that
    survive its free gates. Turning it on hydrates the whole board here
    (the pre-triage behaviour), which spends the host's detail budget on
    rows a title check would have dropped.

    The stats carry net.http.snapshot_info() for this board's own fetch
    (fetch_errors, incomplete, capped, capped_total, last_error); a soft
    failure fills last_error, never `err`, which means an exception.
    """
    t0 = time.monotonic()
    stats: dict[str, Any] = {"name": company.get("name"), "ats": company.get("ats"),
                             "fetched": 0, "new": 0, "hydrated": 0, "unhydrated": 0,
                             "closed": 0, "reopened": 0, "fetch_errors": 0, "err": None,
                             "incomplete": False, "capped": False, "capped_total": None,
                             "secs": 0.0}
    # A fetcher does not RAISE on a dead board -- it reports and returns [],
    # which is also what a board with nothing on it returns, so `fetched: 0`
    # alone cannot tell "404" from "no openings". net.http counts the
    # reported failures per task, and a host's task runs one board at a
    # time, so resetting around the fetch attributes them to this board.
    http.reset_fetch_failures()
    try:
        jobs = await company_fetch.fetch_company(company) or []
    except Exception as e:                      # noqa: BLE001 - reported
        stats["fetch_errors"] = http.fetch_failures()
        stats["incomplete"] = True
        stats["err"] = f"fetch: {type(e).__name__}: {e}"
        stats["secs"] = time.monotonic() - t0
        return stats
    progress()
    stats["fetched"] = len(jobs)
    stats.update(http.snapshot_info())

    try:
        # Bodies already in the store (an earlier harvest, or a crawl) are
        # reused, never re-fetched: many listings come back bodiless every
        # time, and re-hydrating 100 known rows is what tripped one host's
        # limit on a second run (2026-09-10).
        if jobs and company.get("id"):
            stored = await db.run(store.descriptions_for_company, company["id"])
            for j in jobs:
                if not j.get("description") and j["id"] in stored:
                    j["description"] = stored[j["id"]]
        if hydrate:
            await hydrate_rows(jobs, company, stats, delay, backoff_s, progress)
        stamp_dt = now or datetime.now()
        stamp = stamp_dt.isoformat()
        rows = await asyncio.to_thread(
            lambda: [(_row(j, company, stamp), location_unknown(j.get("location")))
                     for j in jobs])
        promoted = await db.batch(_write_board, jobs, rows, company, stats, stamp_dt)
        if promoted:
            print(f"    [!] {company.get('name')}: no jobs for >= "
                  f"{store.HARVEST_DEAD_AFTER_DAYS}d since its first fetch "
                  f"error - promoted to '{promoted}'")
    except Exception as e:                      # noqa: BLE001 - reported
        # A store failure is NOT a fetch error, and calling it one hid this
        # bug for a day: every "database is locked" reads as an unreachable
        # board in the 2026-09-10 logs. The board's postings are simply not
        # written; the next pass re-fetches them.
        stats["err"] = f"store: {type(e).__name__}: {e}"
    stats["secs"] = time.monotonic() - t0
    return stats


def _write_board(conn: sqlite3.Connection, jobs: list[dict[str, Any]],
                 rows: list[tuple[JobIn, bool]], company: CompanyRow,
                 stats: dict[str, Any], stamp_dt: datetime) -> str | None:
    """One board's snapshot written, inside the caller's store.batch (ONE
    transaction, which is the whole point: a board is one lock
    acquisition, not one per posting); `rows` are its (store row,
    keep_location) pairs. Returns mark_harvested's promotion.

    The rows that arrived are always stored. Closing against the snapshot
    is what a partial pull cannot be trusted for: an INCOMPLETE one (a
    fetch failed partway) closes nothing, and neither does a CAPPED one
    (store.sync_job_statuses's `capped`) -- its vanished rows wait for
    ops.check_closed_jobs's URL probe instead.

    A soft-failed snapshot (_soft_failed) is recorded as one:
    store.mark_harvested keeps the last good total_job_count and runs its
    dead-board cycle."""
    if jobs and company.get("id") and not stats.get("incomplete"):
        # A full, successful snapshot is the best evidence there is for
        # what the board lists: close what vanished, revive returners --
        # across every track (track=None), on this pass's own stamp.
        stats["reopened"], stats["closed"] = store.sync_job_statuses(
            conn, company["id"], jobs, track=None,
            capped=stats.get("capped", False), now=stamp_dt)
    for row, keep_location in rows:
        # A whole-board listing can say "N Locations" every pass; the real
        # list triage resolved stays.
        if store.upsert_job(conn, row, keep_location=keep_location):
            stats["new"] += 1
    if not company.get("id"):
        return None
    promoted = store.mark_harvested(conn, company["id"], len(jobs),
                                    soft_fail=_soft_failed(stats), now=stamp_dt)
    # `company` is the pre-pass row: a fetch-error miss already on it means
    # this is the second failing pass in a row.
    if (not promoted and _soft_failed(stats)
            and company.get("miss_reason") == "fetch-error:harvest"):
        bury_404_board(conn, company, stats.get("last_error"))
    return promoted


async def hydrate_rows(jobs: list[dict[str, Any]], company: CompanyRow,
                       stats: dict[str, Any], delay: float | None = None,
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


# --------------------------------------------------------------------------- #
#  The run                                                                     #
# --------------------------------------------------------------------------- #

async def run(db_path: str | Path | None = None, only: Collection[str] | None = None,
              names: Iterable[str] | None = None, min_age_hours: float | None = None,
              limit: int | None = None, max_workers: int = DEFAULT_WORKERS,
              hydrate: bool = False, max_hours: float | None = None,
              board_fn: Callable[..., Awaitable[dict[str, Any]]] = harvest_board,
              triage: bool = True, score_cap: int | None = None) -> dict[str, Any]:
    """Harvest every planned board (`pull`), then triage what was stored
    and rewrite every roster track's digest (`_triage`), in that order.
    Returns the summary dict (also printed); the triage summary rides in it
    under "triage".

    `triage=False` skips the gate/hydrate/score pass (the rows wait for
    the next one, or for `run_scraper.py --triage`); `score_cap` bounds
    that pass's Claude fit calls (triage.SCORE_CAP when None);
    `max_workers` bounds its concurrency. `board_fn` exists for tests (it
    is harvest_board's signature). A cancel (Ctrl+C) is raised here once
    the pass has unwound.
    """
    db_path = db_path or config.STORE_DB_PATH
    summary = await pull(db_path, only=only, names=names,
                         min_age_hours=min_age_hours, limit=limit,
                         hydrate=hydrate, max_hours=max_hours, board_fn=board_fn)
    if triage:
        summary["triage"] = await _triage(db_path, max_workers, score_cap)
    return summary


async def pull(db_path: str | Path, only: Collection[str] | None = None,
               names: Iterable[str] | None = None, min_age_hours: float | None = None,
               limit: int | None = None, hydrate: bool = False,
               max_hours: float | None = None,
               board_fn: Callable[..., Awaitable[dict[str, Any]]] = harvest_board,
               stall_s: float = 900.0) -> dict[str, Any]:
    """The pull: plan (`plan`), then every board fetched and stored, one
    task per host (Board.origin; per platform while a row's origin is
    unsettled) walking its boards in plan order and every host at once;
    the store work on one store.Writer. Returns the summary dict (also
    printed).

    `max_hours` is the pass budget. Past it, or on Ctrl+C, the walks are
    cancelled: the boards committed stay, a board being written rolls
    back, and a board not yet started never starts; the budget names each
    board it cut off (the summary's "abandoned"). A board with no progress
    (harvest_board's) for `stall_s` is cut off the same way and its walk
    moves on: a request bounds each read (config.FETCH_TIMEOUT), not its
    total, so a server trickling bytes could hold one forever.

    The header line names how many off-mission, inactive boards `plan`
    deferred to its longer interval this pass and how many it left out
    (plan's `stats` output; silent when there are none), so a run that
    looks small is never a silent drop -- it says which boards it left
    and why. The open postings of the boards it leaves out as stopped are
    closed first (store.retire_stopped), counted as the summary's
    "retired"; a `names` pass leaves them to the next full one.

    Per-origin TaskGroup walks, not net.parallel.fan_out: see fan_out's
    docstring for why (this pass needs a per-board progress stall and a
    "not started" count).
    """
    summary: dict[str, float] = {"boards": 0, "ok": 0, "err": 0, "abandoned": 0, "dead": 0,
                               "fetched": 0, "new": 0, "hydrated": 0, "closed": 0,
                               "reopened": 0, "retired": 0, "secs": 0.0}
    async with store.Writer(db_path) as db:
        plan_stats: dict[str, Any] = {}
        boards = await db.run(plan, only=only, names=names,
                              min_age_hours=min_age_hours, limit=limit,
                              stats=plan_stats)
        if not names:
            try:
                summary["retired"] = len(await db.run(store.retire_stopped))
            except Exception as e:              # noqa: BLE001 - the next pass retries
                print(f"  [!] stopped boards' postings left open: {type(e).__name__}: {e}")
        hosts: dict[str, list[CompanyRow]] = {}
        for c in boards:
            hosts.setdefault(cast(str, company_fetch.board_origin(c)), []).append(c)
        summary["boards"] = len(boards)
        bar = "=" * 70
        print(f"\n{bar}\n  [HARVEST] whole-board pull - {datetime.now():%Y-%m-%d %H:%M}")
        print(f"  {len(boards)} board(s) on {len(hosts)} host(s), one board at a "
              f"time per host, hydrate={'on' if hydrate else 'off'}, "
              f"skip if harvested < {plan_stats['min_age_hours']:g}h ago"
              + deferred_note(plan_stats)
              + (f", stop after {max_hours:g}h" if max_hours else "") + f"\n{bar}\n")
        if not boards:
            print("  nothing to do" + (f"; {summary['retired']} job(s) closed at "
                                       f"stopped boards" if summary["retired"] else ""))
            return summary

        t_start = time.monotonic()
        running: dict[int, CompanyRow] = {}    # id(company) -> company, mid-walk

        async def walk(group: list[CompanyRow]) -> None:
            loop = asyncio.get_running_loop()
            for c in group:
                running[id(c)] = c
                try:
                    async with asyncio.timeout(stall_s) as stall:
                        s = await board_fn(
                            c, db, hydrate=hydrate,
                            progress=lambda: stall.reschedule(loop.time() + stall_s))
                except Exception as e:          # noqa: BLE001 - reported
                    s = {"err": f"{type(e).__name__}: {e}", "fetched": 0,
                         "new": 0, "hydrated": 0, "closed": 0, "reopened": 0,
                         "secs": 0.0}
                del running[id(c)]
                if stall.expired():
                    print(f"  [!] {c['name']} ({c['ats']}): no progress in "
                          f"{stall_s:g}s - abandoned")
                    summary["abandoned"] += 1
                else:
                    _report(c, s, summary)

        try:
            async with asyncio.timeout(max_hours * 3600 if max_hours else None):
                async with asyncio.TaskGroup() as tg:
                    for group in hosts.values():
                        tg.create_task(walk(group))
        except TimeoutError:
            for c in running.values():
                print(f"  [!] {c['name']} ({c['ats']}): run out of time - abandoned")
            summary["abandoned"] += len(running)
    summary["secs"] = time.monotonic() - t_start
    skipped = summary["boards"] - summary["ok"] - summary["err"] - summary["abandoned"]
    print(f"\n{bar}\n  HARVEST SUMMARY")
    print(f"  boards: {summary['ok']} ok, {summary['err']} failed, "
          f"{summary['abandoned']} abandoned"
          + (f", {summary['dead']} answered with an error and no jobs"
             if summary["dead"] else "")
          + (f", {skipped} not started" if skipped > 0 else ""))
    print(f"  jobs:   {summary['fetched']} fetched, {summary['new']} new, "
          f"{summary['hydrated']} hydrated, {summary['closed']} closed, "
          f"{summary['reopened']} reopened"
          + (f", {summary['retired']} closed at stopped boards"
             if summary["retired"] else ""))
    # Roster hygiene: a board still named after its own slug/tenant
    # fetches fine, so nothing else in the log names it for renaming.
    unnamed = sorted((c for c in boards if slug_named(c)),
                     key=lambda c: -(c.get("total_job_count") or 0))
    print(f"  {len(unnamed)} board(s) still named after their own "
          f"slug/tenant"
          + (f", largest first: {', '.join(c['name'] for c in unnamed[:10])}"
             + (", ..." if len(unnamed) > 10 else "") if unnamed else ""))
    print(f"  time:   {summary['secs'] / 60:.1f} min\n{bar}")
    return summary


def _report(c: CompanyRow, s: dict[str, Any], summary: dict[str, float]) -> None:
    """One finished board's status line, its stats added to `summary`."""
    done = summary["ok"] + summary["err"] + 1
    if s["err"]:
        summary["err"] += 1
    else:
        summary["ok"] += 1
        summary["dead"] += _soft_failed(s)
    for k in ("fetched", "new", "hydrated", "closed", "reopened"):
        summary[k] += s[k]
    prefix = ""
    if s["err"]:
        status = s["err"]
    elif _soft_failed(s):
        # The distinction the log could not previously draw: this board
        # answered with an error, it is not merely empty. A soft
        # failure: it names the last error and leaves s["err"] alone.
        prefix = "[!] "     # session_log records the line at WARNING
        last = f": {s['last_error']}" if s.get("last_error") else ""
        status = (f"no jobs - {s['fetch_errors']} fetch error(s){last}, "
                  f"{s['secs']:.0f}s")
    else:
        status = (f"{s['fetched']} job(s), {s['new']} new, "
                  f"{s['hydrated']} hydrated"
                  + (f" ({s['unhydrated']} unresolved)"
                     if s.get("unhydrated") else "")
                  + f", {s['closed']} closed, {s['secs']:.0f}s")
        if s.get("incomplete"):
            status += " [incomplete: 0 closed]"
        elif s.get("capped"):
            # 2026-09-18: a capped snapshot no longer closes anything
            # here (store.sync_job_statuses) -- ops.check_closed_jobs's
            # URL probe is the only thing that can, later.
            status += (f" [capped of {s['capped_total']}: 0 closed]"
                       if s.get("capped_total") else " [capped: 0 closed]")
    print(f"  {prefix}[{done:>3}/{summary['boards']}] {c['name']} "
          f"({c['ats']}): {status}")
    # Churn a board's own board-diff should have damped, still showing
    # up: worth a human's attention, not just a debug line.
    fetched = s.get("fetched") or 0
    reopened = s.get("reopened") or 0
    if reopened > max(10, 0.05 * fetched):
        print(f"    [!] {c['name']} ({c['ats']}): {reopened} reopened "
              f"of {fetched} fetched - board churning")
    _log.debug("board %s stats %s", c.get("name"), s)


async def _triage(db_path: str | Path, max_workers: int,
                  score_cap: int | None) -> dict[str, Any]:
    """The pass's second half; returns triage.run's summary.

    The gate/hydrate/score pass over everything pending in the store --
    this pass's rows and any earlier pass left waiting on a body or over
    the scoring cap -- then, on that same `db_path`: each roster track's
    deep verify (ops.verify_top, within its verify_top), one closed-URL
    probe (ops.check_closed_jobs: CLOSED_PROBE_LIMIT open rows no board
    has vouched for in CLOSED_PROBE_STALE_DAYS), each roster track's
    digest (no email), and the Claude spend footer for the pass's calls.
    With no API key, or the breaker already tripped, the verify step is
    one printed line instead.

    Notes:
        triage is imported here because it imports this module. The steps
        after it use `db_path`, not maintenance.track_writer(t): that opens
        each track's configured store, a different file whenever `db_path`
        is overridden (tests, `harvest.py --db`), and triage.run reads
        every roster track from the one `db_path` too. The verify guard
        sits here so a dead API prints one line, not one per track.
    """
    from src.crawl import triage
    kw: dict[str, Any] = {"score_cap": score_cap} if score_cap is not None else {}
    result = await triage.run(db_path=db_path, max_workers=max_workers, **kw)
    async with store.Writer(db_path) as db:
        tracks = triage.roster_tracks()
        down = api_disabled()
        if not have_api_key() or down:
            print(f"\n  [!] verify skipped for {len(tracks)} roster track(s): "
                  f"{down or 'no ANTHROPIC_API_KEY configured'}")
        else:
            for t in tracks:
                if t.verify_top:
                    await verify_top(top_n=t.verify_top,
                                     max_workers=max(2, max_workers // 2),
                                     db=db, t=t)
        await check_closed_jobs(limit=CLOSED_PROBE_LIMIT,
                                stale_days=CLOSED_PROBE_STALE_DAYS, db=db)
        for t in tracks:
            await db.run(rewrite_digest, t, top_n=5,
                         heading=f"\n  [{t.track}] digest rewritten:")
    report_cache_stats()
    return result
