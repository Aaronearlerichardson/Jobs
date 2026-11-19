"""Background whole-board harvester: fetch EVERY board, store everything,
score nothing.

A crawl of one track (`run_track`) is this same pipeline scoped to
that track: `run`'s `scope` narrows the boards pulled to its store tag, the
feeds to the ones it reads, and the tracks verified, digested and announced
to itself. Triage still judges every track that reads harvested rows.

Unscoped, it is a slow, thorough pass that runs in the
background (Task Scheduler at log-on, repeating every few hours) and

  * takes every company with a fetchable board (store.harvestable_companies:
    active or not, any tag -- only rows with no board, or a
    blocklisted name, are skipped), except an inactive one mission-scored
    off-mission (plan), whose open postings the pass closes instead
    (store.retire_stopped);
  * pulls the WHOLE board, no location filter, as a bodiless listing;
  * stores the rows unscored and untracked (jobs.harvested_at stamped), and
    reconciles open/closed status against the full snapshot;
  * pulls the FEEDS the tracks read (RemoteOK, Remotive, HN who-is-hiring,
    RSS, Discourse, USAJOBS, Getro) the same way, ungated: each feed
    instance is a roster row (store.sync_feed_companies), planned and
    stored like a board;
  * then hands everything pending to the triage pass (src/crawl/triage.py),
    which runs the track gates cheapest-first, hydrates only the
    survivors, and fit-scores only the hydrated survivors.

Triage is where the Claude spend is: one mission call per never-scored
company and one fit call per posting that clears four free gates. A row
triage surfaces carries its track labels and score; a row it drops
records the gate that dropped it.

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
that host's boards one at a time, least recently attempted first, and every
host runs at once, so a board's host never sees two of the pass's boards at
a time.
Ctrl+C or the pass budget cancels it: committed boards stay, a board
still being written rolls back, and no queued board starts.
A board that makes no progress for pull's `stall_s` is cut off alone.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from contextlib import closing
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import cast

from typing_extensions import TypedDict

from src import config
from src import digest
from src import store
from src.ats.board import company as company_fetch
from src.ats.coords import slug_named
from src.claude.api import api_disabled, have_api_key, report_cache_stats
from src.claude.resume import resume_text
from src.config import RuntimeTrack
from src.crawl import health, triage
from src.crawl.hydrate import MISS_BACKOFF_S, BoardStats, hydrate_rows
from src.crawl.triage import DEFAULT_WORKERS
from src.discovery.apply import attribute_employers
from src.match.locality import geo_mode, location_unknown, remote_signal_for
from src.net import http
from src.ops.ingest import add_manual_job
from src.ops.maintenance import new_watch_hits, rewrite_digest, track_writer
from src.ops.repair import bury_404_board
from src.ops.scoring import self_heal_unscored, verify_top
from src.ops.status import check_closed_jobs
from src.rows import CompanyRow, FetchedJob, JobIn

_log = logging.getLogger(__name__)

# The post-triage closed-URL probe (ops.check_closed_jobs): how stale a
# tracked OPEN row has to be (no board has vouched for it in this many
# days) before its detail URL is worth a live GET, and how many such probes
# one pass spends. A host takes at most config.CLOSED_PROBE_PER_HOST of
# them, paced, so this bounds the pass's total, not its time.
CLOSED_PROBE_STALE_DAYS = 7
CLOSED_PROBE_LIMIT = 500


class PlanStats(TypedDict, total=False):
    """What `plan` reports about the boards it left out."""
    offmission_skipped: int
    offmission_stopped: int
    min_age_hours: float


# --------------------------------------------------------------------------- #
#  Planning                                                                    #
# --------------------------------------------------------------------------- #

def deferred_note(stats: PlanStats) -> str:
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


def _read(c: CompanyRow, scope: RuntimeTrack | None) -> bool:
    """Whether a pull scoped to `scope` reads roster row `c`. Unscoped, every
    board of the store, and a feed's row when some track reads its family."""
    if scope:
        return triage.track_reads(scope, c)
    return (config.row_family(c.get("ats")) == "store"
            or any(triage.track_reads(t, c) for t in triage.roster_tracks()))


def plan(conn: sqlite3.Connection, only: Collection[str] | None = None,
         names: Iterable[str] | None = None, min_age_hours: float | None = None,
         limit: int | None = None, now: datetime | None = None,
         stats: PlanStats | None = None, scope: RuntimeTrack | None = None) -> list[CompanyRow]:
    """The boards (and feeds: their roster rows) this run will pull, in run order.

    `only` restricts to a set of ATS names, `names` to company names
    (case-insensitive), `scope` to the rows that track reads
    (triage.track_reads; unscoped: every store board, and the feeds some
    track reads); boards harvested within `min_age_hours` are skipped
    unless named explicitly. Least recently ATTEMPTED first
    (harvest_attempted_at, which pull stamps as each board starts; never
    attempted first), then largest, then by name: each host walks its boards
    in this order, so the boards a budget or `limit` left out lead the next
    pass.

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
    ...         "handle": "big|5|Ext"})
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

    A `scope` with a `store_tag` keeps only the boards carrying it, and a
    feed's row only when the track reads its family:

    >>> from src.config.profile_schema import TrackSources
    >>> _ = store.upsert_company(conn, {"name": "Tagged", "ats": "lever",
    ...                                 "slug": "tg", "tags": "sweep"})
    >>> _ = store.upsert_company(conn, {"name": "Feed", "ats": "remoteok", "slug": "remoteok"})
    >>> t = RuntimeTrack(id="t", db_path=Path("t.db"), store_tag="sweep",
    ...                  sources=TrackSources(aggregators=False))
    >>> [c["name"] for c in plan(conn, scope=t, now=now)]
    ['Tagged']
    >>> from unittest import mock
    >>> on = {"remoteok": {**config.FEEDS["remoteok"], "on": True}}
    >>> reads = t.model_copy(update={"sources": TrackSources(aggregators=True)})
    >>> with mock.patch.dict(config.FEEDS, on):
    ...     [c["name"] for c in plan(conn, scope=reads, now=now)]
    ['Feed', 'Tagged']

    A pass that fits k boards takes the k stalest, the next pass the next
    k, so every board is attempted within ceil(n/k) passes:

    >>> conn = store.connect(":memory:")
    >>> for i, at in enumerate(["2026-09-05", None, "2026-09-01", "2026-09-03", None]):
    ...     _ = store.upsert_company(conn, {"name": f"B{i}", "ats": "lever", "slug": f"b{i}"})
    ...     _ = conn.execute("UPDATE companies SET harvest_attempted_at=? WHERE name=?",
    ...                      (at, f"B{i}"))
    >>> for p in range(3):
    ...     batch = plan(conn, min_age_hours=0, limit=2, now=now)
    ...     print([c["name"] for c in batch])
    ...     for c in batch:
    ...         store.mark_harvest_attempted(conn, c["id"], now + timedelta(hours=p))
    ['B1', 'B4']
    ['B2', 'B3']
    ['B0', 'B1']

    Notes:
        The 2026-09-17 audit counted 289 off-mission inactive boards
        holding 51,134 stored jobs, against 288 active ones (any tier)
        holding 47,737 -- so roughly half of every whole-board pass was
        spent re-fetching rows triage's own mission gate discards on every
        single run.

        Until 2026-09-25 the cheap sweep platforms went first, then the
        smaller boards, for a thread pool that banked the most boards per
        minute; then largest first until 2026-10-07, which left the same
        small boards at the tail of every walk a budget cut short. A host's
        walk is sequential, so its order never changes its length.
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
        if not _read(c, scope):
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
    rows.sort(key=lambda c: (c.get("harvest_attempted_at") or "",
                             -(c.get("total_job_count") or 0),
                             (c.get("name") or "").lower()))
    if stats is not None:
        stats["offmission_skipped"] = offmission_skipped
        stats["offmission_stopped"] = offmission_stopped
        stats["min_age_hours"] = min_age_hours
    return rows[:limit] if limit else rows


def list_plan(db_path: str | Path | None, only: Collection[str] | None = None,
              names: Iterable[str] | None = None, min_age_hours: float | None = None,
              limit: int | None = None, *,
              scope: RuntimeTrack | None = None) -> None:
    """Print the boards and feeds `run` would pull from the store at
    `db_path` for the same arguments, one line each, and fetch nothing
    (harvest.py --list; a previewed crawl). The feeds' roster rows are
    synced first, so a store that has not been pulled yet lists them."""
    stats: PlanStats = {}
    with closing(store.connect(db_path)) as conn:
        store.sync_feed_companies(conn)
        boards = plan(conn, only=only, names=names, min_age_hours=min_age_hours,
                      limit=limit, stats=stats, scope=scope)
    stores = [c for c in boards if config.row_family(c.get("ats")) == "store"]
    for c in boards:
        print(f"  {c['name']}  ({c['ats']}, ~{c.get('total_job_count') or 0}"
              f" jobs, last harvested {c.get('last_harvested_at') or 'never'})")
    print(f"  {len(stores)} board(s)" + deferred_note(stats))
    if len(stores) < len(boards):
        print(f"  {len(boards) - len(stores)} feed(s)")


# --------------------------------------------------------------------------- #
#  One board                                                                   #
# --------------------------------------------------------------------------- #

def _row(job: FetchedJob, company: CompanyRow, stamp: str) -> JobIn:
    """The store row for one harvested posting: identity, body, dates -- no
    track, no score. A feed's posting (company.ats a feed family) names its
    own employer, and carries the remote signal the fetcher's hint or its
    text gives, which triage cannot recover from the row.

    >>> feed = {"id": 7, "ats": "remoteok", "name": "remoteok feed"}
    >>> row = _row({"id": "r1", "company": "Zed", "title": "T", "remote_hint": "board:remoteok"}, feed, "s")
    >>> row["company_id"], row["company_name"], row["remote_signal"]
    (7, 'Zed', 'hint:board:remoteok')
    >>> row = _row({"id": "g1", "company": "Zed", "title": "T", "remote_hint": "x"},
    ...            {"id": 8, "ats": "lever", "name": "Acme"}, "s")
    >>> row["company_name"], "remote_signal" in row
    ('Acme', False)

    Notes:
        A fetcher's remote hint is stamped at store time for a feed's rows
        only. Stamping every row that carries one (the board engines give
        hints too) would move board rows' stored remote_eligible and
        remote_signal from triage's text reading to the hint, so it is kept.
    """
    desc = (job.get("description") or "")[:config.MAX_DESC_CHARS]
    feed = config.row_family(company.get("ats")) != "store"
    row: JobIn = {
        "job_id": job["id"],
        "company_id": company.get("id"),
        "company_name": job.get("company") if feed else company.get("name"),
        "title": job.get("title"),
        "url": job.get("url"), "location": job.get("location"),
        "geo_mode": geo_mode(job.get("location", ""), desc),
        "posted_at": job.get("posted_at"), "description": desc,
        "harvested_at": stamp,
    }
    if feed:
        sig = remote_signal_for(job)
        row.update(remote_eligible=bool(sig), remote_signal=sig)
    return row


def _soft_failed(stats: BoardStats) -> bool:
    """A board that answered with an error rather than an empty one: no
    rows, and its own fetch reported failures (net.http.snapshot_info).

    >>> _soft_failed({"fetched": 0, "fetch_errors": 2})
    True
    >>> _soft_failed({"fetched": 0, "fetch_errors": 0})
    False
    """
    return not stats.get("fetched") and bool(stats.get("fetch_errors"))


def _stats(name: str | None, ats: str | None) -> BoardStats:
    """The stats a board's (or feed's) pull starts from."""
    return {"name": name, "ats": ats, "fetched": 0, "new": 0, "hydrated": 0,
            "unhydrated": 0, "closed": 0, "reopened": 0, "fetch_errors": 0,
            "err": None, "incomplete": False, "capped": False,
            "capped_total": None, "secs": 0.0}


async def _snapshot(fetch: Callable[[], Awaitable[list[FetchedJob] | None]],
                    stats: BoardStats,
                    progress: Callable[[], object]) -> list[FetchedJob] | None:
    """`fetch`'s rows, with the fetch accounting (net.http.snapshot_info)
    added to `stats`; None, `stats` filled, when it raised. `progress()`
    is called when the listing is back.

    A fetcher does not RAISE on a dead board -- it reports and returns [],
    which is also what a board with nothing on it returns, so `fetched: 0`
    alone cannot tell "404" from "no openings". net.http counts the
    reported failures per task, and a host's task runs one board at a
    time, so resetting around the fetch attributes them to this board.
    """
    http.reset_fetch_failures()
    try:
        jobs = await fetch() or []
    except Exception as e:                      # noqa: BLE001 - reported
        stats["fetch_errors"] = http.fetch_failures()
        stats["incomplete"] = True
        stats["err"] = f"fetch: {type(e).__name__}: {e}"
        return None
    progress()
    stats["fetched"] = len(jobs)
    stats.update(http.snapshot_info())
    return jobs


async def harvest_board(company: CompanyRow, db: store.Writer, hydrate: bool = False,
                        delay: float | None = None, now: datetime | None = None,
                        backoff_s: float = MISS_BACKOFF_S,
                        progress: Callable[[], object] = lambda: None) -> BoardStats:
    """Fetch, hydrate and store ONE board, its store work on `db` (the
    pass's store.Writer). Returns a stats dict; a fetch failure is
    reported, not raised, and leaves the store untouched (nothing is
    closed on a failed or empty snapshot; see _write_rows for an
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
    stats = _stats(company.get("name"), company.get("ats"))
    jobs = await _snapshot(lambda: company_fetch.fetch_company(company, employer=company["name"]),
                           stats, progress)
    if jobs is None:
        stats["secs"] = time.monotonic() - t0
        return stats

    async def prepare() -> list[FetchedJob]:
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
        if any(j.get("_employer") for j in jobs):
            return await db.run(attribute_employers, jobs, commit=True)
        return jobs

    promoted = await _store_snapshot(db, jobs, stats, t0, now, prepare,
                                     partial(_write_board, company=company), company=company)
    if promoted:
        print(f"    [!] {company.get('name')}: no jobs for >= "
              f"{store.HARVEST_DEAD_AFTER_DAYS}d since its first fetch "
              f"error - promoted to '{promoted}'")
    return stats


async def _store_snapshot(db: store.Writer, jobs: list[FetchedJob], stats: BoardStats,
                          t0: float, now: datetime | None,
                          prepare: Callable[[], Awaitable[list[FetchedJob]]],
                          write: Callable[..., str | None], *,
                          company: CompanyRow) -> str | None:
    """`prepare`d rows stamped and written by `write` in ONE store.batch
    (a board is one lock acquisition, not one per posting); the snapshot
    closed against is all of `jobs`. `write` returns mark_harvested's
    promotion, if any. A store failure is reported in `stats`, with
    `secs`, not raised."""
    try:
        kept = await prepare()
        stamp_dt = now or datetime.now()
        stamp = stamp_dt.isoformat()
        rows = await asyncio.to_thread(
            lambda: [(_row(j, company, stamp), location_unknown(j.get("location")))
                     for j in kept])
        return await db.batch(write, jobs, rows, stats, stamp_dt)
    except Exception as e:                      # noqa: BLE001 - reported
        # A store failure is NOT a fetch error, and calling it one hid this
        # bug for a day: every "database is locked" reads as an unreachable
        # board in the 2026-09-10 logs. The board's postings are simply not
        # written; the next pass re-fetches them.
        stats["err"] = f"store: {type(e).__name__}: {e}"
        return None
    finally:
        stats["secs"] = time.monotonic() - t0


def _write_rows(conn: sqlite3.Connection, jobs: list[FetchedJob],
                rows: list[tuple[JobIn, bool]], stats: BoardStats, stamp_dt: datetime,
                *, company_id: int | None = None) -> None:
    """One snapshot's rows written, inside the caller's store.batch;
    `rows` are its (store row, keep_location) pairs.

    The rows that arrived are always stored. Closing against the snapshot
    is what a partial pull cannot be trusted for: an INCOMPLETE one (a
    fetch failed partway) closes nothing, and neither does a CAPPED one
    (store.sync_job_statuses's `capped`) -- its vanished rows wait for
    ops.check_closed_jobs's URL probe instead."""
    if jobs and company_id and not stats.get("incomplete"):
        # A full, successful snapshot is the best evidence there is for
        # what the board lists: close what vanished, revive returners --
        # across every track (track=None), on this pass's own stamp.
        stats["reopened"], stats["closed"] = store.sync_job_statuses(
            conn, company_id, jobs, track=None,
            capped=stats.get("capped", False), now=stamp_dt)
    for row, keep_location in rows:
        # A whole-board listing can say "N Locations" every pass; the real
        # list triage resolved stays.
        if store.upsert_job(conn, row, keep_location=keep_location):
            stats["new"] += 1


def _write_board(conn: sqlite3.Connection, jobs: list[FetchedJob],
                 rows: list[tuple[JobIn, bool]], stats: BoardStats, stamp_dt: datetime,
                 *, company: CompanyRow) -> str | None:
    """One board's snapshot written (`_write_rows`), then recorded in the
    same transaction. Returns mark_harvested's promotion.

    A soft-failed snapshot (_soft_failed) is recorded as one:
    store.mark_harvested keeps the last good total_job_count and runs its
    dead-board cycle."""
    company_id = company.get("id")
    _write_rows(conn, jobs, rows, stats, stamp_dt, company_id=company_id)
    if not company_id:
        return None
    promoted = store.mark_harvested(conn, company_id, len(jobs),
                                    soft_fail=_soft_failed(stats), now=stamp_dt)
    # A definitive "no such board" answer (404/410) buries at once; any
    # other failure waits out store.HARVEST_DEAD_AFTER_DAYS.
    if not promoted and _soft_failed(stats):
        bury_404_board(conn, company, stats.get("last_error"))
    return promoted


# --------------------------------------------------------------------------- #
#  The run                                                                     #
# --------------------------------------------------------------------------- #

async def run(db_path: str | Path | None = None, only: Collection[str] | None = None,
              names: Iterable[str] | None = None, min_age_hours: float | None = None,
              limit: int | None = None, max_workers: int = DEFAULT_WORKERS,
              hydrate: bool = False, max_hours: float | None = None,
              board_fn: Callable[..., Awaitable[BoardStats]] = harvest_board,
              triage: bool = True, score_cap: int | None = None, *,
              scope: RuntimeTrack | None = None,
              fit: bool = True, top_n: int = 5) -> dict[str, float | Mapping[str, object]]:
    """Harvest every planned board and feed (`pull`), then triage what was
    stored and report (`_triage`), in that order. Returns the summary dict
    (also printed); the triage summary rides in it under "triage".

    A scoped run (a crawl of one track) sets `scope`: only the boards
    carrying its `store_tag` and the feeds it reads are pulled, and only
    it is verified, self-healed, digested and announced, by its own
    verify_top and email. None (the default) pulls every board and every
    feed some track reads, and reports every track that reads harvested
    rows. Triage still judges all of them, scoring `scope`'s rows first.
    A run given `names` is targeted:
    it pulls those boards, triages only their companies' rows, and rewrites
    and announces the digest, but skips every store-wide step (company and
    job dedup, stopped-board retirement, self-heal, deep verify, the
    closed-URL probe). `fit=False` scores nothing;
    `top_n` is how many of each digest are printed.

    `triage=False` skips the gate/hydrate/score pass (the rows wait for
    the next one, or for `run_scraper.py --triage`); `score_cap`
    (triage.SCORE_CAP when None) is the run's one budget of fit-score
    calls, refused ones included: triage takes what it needs, the
    self-heal what is left. Mission scoring and deep verify keep their
    own limits and draw nothing from it.
    `max_workers` bounds the concurrency. `board_fn` exists for tests (it
    is harvest_board's signature). A cancel (Ctrl+C) is raised here once
    the pass has unwound.
    """
    db_path = db_path or config.STORE_DB_PATH
    since = datetime.now().isoformat()
    names = list(names or ())
    targeted = any(n.strip() for n in names)
    if not targeted:
        async with store.Writer(db_path) as db:
            merged = await db.run(store.dedup_companies)
        if merged:
            print(f"  merged {merged} duplicate company row(s) that shared a board")
    summary = await pull(db_path, only=only, names=names,
                         min_age_hours=min_age_hours, limit=limit,
                         hydrate=hydrate, max_hours=max_hours, board_fn=board_fn,
                         scope=scope)
    company_ids = None
    if targeted:
        async with store.Writer(db_path) as db:
            company_ids = [c["id"] for c in await db.run(plan, names=names, min_age_hours=0)]
    else:
        async with store.Writer(db_path) as db:
            flagged = await db.run(store.flag_duplicate_jobs)
        if flagged:
            print(f"  {flagged} cross-board duplicate flag(s) changed")
    out: dict[str, float | Mapping[str, object]] = {**summary}
    if triage:
        out["triage"] = await _triage(
            db_path, max_workers, score_cap, since, scope=scope, fit=fit, top_n=top_n,
            company_ids=company_ids)
    return out


async def run_track(t: RuntimeTrack, *, fit: bool = True, commit: bool = True,
                    send: bool | None = None, verify: bool | None = None,
                    refresh: bool = False, max_workers: int = 6,
                    top_n: int = 15) -> None:
    """Run one crawl of track `t` (a config.UI_TRACKS entry): `run` scoped
    to it, every switch read from the track config. The keyword args only
    OVERRIDE it for this run (None = use the config): `send` overrides
    t.email, `verify` (bool) overrides t.verify_top (True -> top_n, False
    -> skip). `refresh` pulls every board, not only the ones not pulled in
    the last few hours. `fit=False` skips resume scoring; `commit=False`
    prints the boards and feeds the crawl would pull and stops.
    """
    if not commit:
        list_plan(t.db_path, min_age_hours=0 if refresh else None, scope=t)
        return
    n = t.verify_top if verify is None else (top_n if verify else 0)
    await run(db_path=t.db_path,
              scope=t.model_copy(update={"email": t.email if send is None else send,
                                         "verify_top": n}),
              min_age_hours=0 if refresh else None, fit=fit,
              top_n=top_n, max_workers=max_workers)


async def sync_statuses(t: RuntimeTrack, top_n: int = 15) -> None:
    """Status-only reconciliation of track `t`: pull every board and feed
    in its scope now (closing what vanished, reopening what returned), no
    triage and no scoring, then rewrite its digest from the corrected
    ranking.
    """
    await run(db_path=t.db_path, scope=t, triage=False, min_age_hours=0)
    async with track_writer(t) as db:
        await db.run(rewrite_digest, t, top_n,
                     lambda n_open: f"\n  {n_open} open job(s) in ranking.")



async def add_job(url: str = "", title: str = "", company: str = "", location: str = "",
                  description: str = "", pull_board: bool = True,
                  t: RuntimeTrack | None = None) -> dict[str, int | bool | str]:
    """Add one job by hand (`add_manual_job`), then, when its company has a
    board, pull the whole board and triage it under the run's budget.
    Returns add_manual_job's summary."""
    t = t or config.track_for_engine("local")
    out = await add_manual_job(url, title, company, location, description, t=t)
    if pull_board and out.get("board"):
        await run(db_path=t.db_path, scope=t, names=[str(out["company"])], min_age_hours=0)
    return out


async def pull(db_path: str | Path, only: Collection[str] | None = None,
               names: Iterable[str] | None = None, min_age_hours: float | None = None,
               limit: int | None = None, hydrate: bool = False,
               max_hours: float | None = None,
               board_fn: Callable[..., Awaitable[BoardStats]] = harvest_board,
               stall_s: float = 900.0, *,
               scope: RuntimeTrack | None = None) -> dict[str, float]:
    """The pull of `scope`'s boards and feeds: sync the feeds' roster rows
    (store.sync_feed_companies), plan (`plan`; a feed is a row like a board,
    counted among the summary's "boards"), then every board fetched and
    stored, one task per host (Board.origin; per platform, or feed family,
    while a row's origin is unsettled) walking its boards in plan order and
    every host at once; the store work on one store.Writer. Returns the summary
    dict (also printed). Each board's harvest_attempted_at is stamped as
    its walk starts it (plan's rotation key).

    `max_hours` is the pass budget. Past it, or on Ctrl+C, the walks are
    cancelled: the boards committed stay, a board being written rolls
    back, and a board not yet started never starts; the budget names each
    board it cut off (the summary's "abandoned"). A board or feed with no
    progress (harvest_board's) for `stall_s` is cut off the same way and
    its walk moves on: a request bounds each read (config.FETCH_TIMEOUT),
    not its total, so a server trickling bytes could hold one forever.

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
        await db.run(store.sync_feed_companies)
        plan_stats: PlanStats = {}
        boards = await db.run(plan, only=only, names=names,
                              min_age_hours=min_age_hours, limit=limit,
                              stats=plan_stats, scope=scope)
        feeds = sum(config.row_family(c.get("ats")) != "store" for c in boards)
        plan_stats.setdefault("min_age_hours", 0.0)
        if (scope is None or scope.sources.store) and not names:
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
        print(f"  {len(boards) - feeds} board(s)"
              + (f" and {feeds} feed(s)" if feeds else "")
              + f" on {len(hosts)} host(s), one board at a "
              f"time per host, hydrate={'on' if hydrate else 'off'}, "
              f"skip if harvested < {plan_stats['min_age_hours']:g}h ago"
              + deferred_note(plan_stats)
              + (f", stop after {max_hours:g}h" if max_hours else "") + f"\n{bar}\n")
        if not boards:
            print("  nothing to do" + (f"; {summary['retired']} job(s) closed at "
                                       f"stopped boards" if summary["retired"] else ""))
            return summary

        t_start = time.monotonic()
        running: dict[int, str] = {}    # id(unit) -> "name (kind)", mid-walk
        tally = health.Tally("harvest")

        async def one(unit: object, name: str, kind: str,
                      call: Callable[[Callable[[], object]], Awaitable[BoardStats]]
                      ) -> BoardStats | None:
            """`call` (given the stall-bound's progress callback) on one board
            or feed, reported; None when the stall bound cut it off."""
            loop = asyncio.get_running_loop()
            running[id(unit)] = f"{name} ({kind})"
            stall = asyncio.timeout(stall_s)
            try:
                async with stall:
                    s = await call(lambda: stall.reschedule(loop.time() + stall_s))
            except Exception as e:              # noqa: BLE001 - reported
                s = _stats(name, kind) | {"err": f"{type(e).__name__}: {e}"}
            del running[id(unit)]
            if stall.expired():
                print(f"  [!] {name} ({kind}): no progress in {stall_s:g}s - abandoned")
                summary["abandoned"] += 1
                return None
            _report(name, kind, s, summary)
            return s

        async def walk(group: list[CompanyRow]) -> None:
            for c in group:
                async def pull_board(progress: Callable[[], object], c: CompanyRow = c) -> BoardStats:
                    # Stamped as it starts, so a board that fails, wedges
                    # or runs into the budget rotates back; one never
                    # started keeps its place at the front (plan).
                    await db.run(store.mark_harvest_attempted, c["id"])
                    return await board_fn(c, db, hydrate=hydrate, progress=progress)
                s = await one(c, c["name"], c["ats"] or "", pull_board)
                if s is not None:
                    tally.note(c["ats"] or "", s["fetched"], s.get("fill") or {},
                               err=s["err"], snap=s)

        try:
            async with asyncio.timeout(max_hours * 3600 if max_hours else None):
                async with asyncio.TaskGroup() as tg:
                    for group in hosts.values():
                        tg.create_task(walk(group))
        except TimeoutError:
            for label in running.values():
                print(f"  [!] {label}: run out of time - abandoned")
            summary["abandoned"] += len(running)
        for line in await db.run(health.record, tally):
            print(f"  [!] {line}")
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


def _report(name: str, kind: str, s: BoardStats, summary: dict[str, float]) -> None:
    """One finished board's (or feed's) status line, its stats added to
    `summary`; `kind` is the board's ATS, or "feed"."""
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
    print(f"  {prefix}[{done:>3}/{summary['boards']}] {name} "
          f"({kind}): {status}")
    # Churn a board's own board-diff should have damped, still showing
    # up: worth a human's attention, not just a debug line.
    fetched = s.get("fetched") or 0
    reopened = s.get("reopened") or 0
    if reopened > max(10, 0.05 * fetched):
        print(f"    [!] {name} ({kind}): {reopened} reopened "
              f"of {fetched} fetched - board churning")
    _log.debug("board %s stats %s", name, s)


async def _triage(db_path: str | Path, max_workers: int, score_cap: int | None, since: str, *,
                  scope: RuntimeTrack | None, fit: bool, top_n: int,
                  company_ids: Collection[int] | None = None
                  ) -> Mapping[str, object]:
    """The pass's second half: triage.run over everything pending (its
    summary is returned), then for `scope` (default: every track that
    reads harvested rows) self-heal on the budget left, deep verify, one
    closed-URL probe (`_heal_verify_probe`), the digest (watch section:
    config.WATCH_WINDOW_DAYS), the announcement of what surfaced since
    `since`, and the Claude spend footer. A targeted run (`company_ids`
    given) triages only those companies' rows and skips self-heal, verify
    and the probe.

    Notes:
        The steps after triage use `db_path`, not maintenance.track_writer(t): that opens
        each track's configured store, a different file whenever `db_path`
        is overridden (tests, `harvest.py --db`), and triage.run reads
        every roster track from the one `db_path` too. The verify guard
        sits here so a dead API prints one line, not one per track.
    """
    cap = triage.SCORE_CAP if score_cap is None else score_cap
    tracks = [scope] if scope else triage.roster_tracks()
    result = await triage.run(db_path=db_path, max_workers=max_workers,
                              score_cap=cap, fit=fit,
                              prefer={scope.track} if scope else (),
                              company_ids=company_ids)
    left = cap - cast(int, result.get("attempted", 0))
    async with store.Writer(db_path) as db:
        if company_ids is None:
            await _heal_verify_probe(db, tracks, left, max_workers, fit)
        pipeline, followups = await db.run(
            lambda conn: (store.get_pipeline(conn), store.followups_due(conn)))
        window = (datetime.now() - timedelta(days=config.WATCH_WINDOW_DAYS)).isoformat()
        for t in tracks:
            hits = await db.run(new_watch_hits, t, window)
            ranked = await db.run(rewrite_digest, t, top_n=top_n, watch_hits=hits,
                                  heading=f"\n  [{t.track}] digest rewritten:")
            await asyncio.to_thread(digest.announce, ranked, t, since,
                                    pipeline=pipeline, followups=followups,
                                    watch_hits=hits)
    report_cache_stats()
    return result


async def _heal_verify_probe(db: store.Writer, tracks: list[RuntimeTrack], left: int,
                             max_workers: int, fit: bool) -> None:
    """The store-wide steps after triage: per track self-heal on the budget
    `left`, deep verify, then one closed-URL probe. With fit scoring off, no
    API key, or the breaker tripped, self-heal and verify are one printed
    line instead."""
    down = api_disabled()
    why = ("fit scoring off" if not fit else
           down or (None if have_api_key() else "no ANTHROPIC_API_KEY configured"))
    if why:
        print(f"\n  [!] verify skipped for {len(tracks)} report track(s): {why}")
    else:
        resume = await asyncio.to_thread(resume_text)
        for t in tracks:
            if resume and left > 0:
                left -= await self_heal_unscored(db, resume, track=t.track,
                                                 max_workers=max_workers, limit=left)
            if t.verify_top:
                await verify_top(top_n=t.verify_top, max_workers=max(2, max_workers // 2),
                                 db=db, t=t)
    await check_closed_jobs(limit=CLOSED_PROBE_LIMIT,
                            stale_days=CLOSED_PROBE_STALE_DAYS, db=db)
