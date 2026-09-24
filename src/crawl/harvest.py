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
    active or not, dormant or not, any tag, any mission score -- only rows
    with no board, or a blocklisted name, are skipped);
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
board takes the write lock once rather than a thousand times.

Wall clock is the only thing this trades away: the whole roster is on the
order of 20,000 postings, and even the listings alone take a while at
polite pacing (with --hydrate, every posting's detail GET on top of that
is hours). Boards run concurrently (one worker per board; a board's own
requests stay serial, which is the per-host politeness), cheapest ATSes
first so the store fills early, and a board that makes no progress for
STALL_S is abandoned rather than allowed to wedge the run.
"""

import logging
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as fut_wait
from contextlib import closing
from datetime import datetime, timedelta

from src import config
from src import store
from src.ats.coords import slug_named
from src.ats.fetchers import company as company_fetch
from src.ats.registry import LIGHTWEIGHT
from src.claude.api import (api_disabled, cache_stats, have_api_key,
                            report_cache_stats)
from src.match.locality import geo_mode, location_unknown
from src.net import http
from src.net.util import worker_count
from src.ops.maintenance import check_closed_jobs, rewrite_digest, verify_top

_log = logging.getLogger(__name__)

# Boards in flight at once. Each board is one host, and a board's own
# requests are serial, so this is also the per-host politeness bound.
DEFAULT_WORKERS = worker_count("HARVEST_WORKERS")
# Hydration is a detail GET per posting, config.HYDRATE_DELAY_S apart and
# at most config.HYDRATE_CAP_PER_RUN per board per run: one host cut the
# crawler off after 151 detail GETs at two per second (2026-09-10), and
# again after 42 on a retry ten minutes later. The rows left bodiless are
# picked up by later runs (stored bodies are never re-fetched, so each run
# advances).
# Consecutive hydration misses that mean the host has stopped answering
# (some drop the connection outright once they decide you are a bot).
# The first streak earns one pause-and-retry; a second ends hydration for
# this board, and the next run picks the bodiless rows up again.
MISS_STREAK = 5
MISS_BACKOFF_S = 90.0
# A board that has made NO progress (no fetch return, no hydrated row) for
# this long is abandoned. Generous: one detail GET is bounded by
# config.FETCH_TIMEOUT, so only a wedged fetcher gets here.
STALL_S = 900.0
# A board harvested more recently than this is skipped, which is what makes
# a restarted (or Task-Scheduler-repeated) run resume where it left off.
# A board that is BOTH off-mission and inactive waits the longer
# config.HARVEST_OFFMISSION_HOURS instead -- see plan()'s docstring.
MIN_AGE_HOURS = 6.0
# The post-triage closed-URL probe (ops.check_closed_jobs): how stale a
# tracked OPEN row has to be (no board has vouched for it in this many
# days) before its detail URL is worth a live GET, and how many such probes
# one pass spends -- the harvester's own per-board caps bound the fetch
# side, this bounds the probe side the same way.
CLOSED_PROBE_STALE_DAYS = 7
CLOSED_PROBE_LIMIT = 100
# ATS families whose public boards API answers 404 only for a board that
# does not exist, so a second one is a verdict, not a blip. Greenhouse
# harvard and cognitotherapeutics 404'd in the 2026-09-22 harvest AND in
# both web-UI crawls after it, each a live GET the three-day grace
# (store.HARVEST_DEAD_AFTER_DAYS) would have kept spending. Workday is out:
# its tenants 404 transiently. The crawl (src.crawl.runner) reads this too.
DEFINITIVE_404_ATS = frozenset(
    ats for ats, spec in config.BOARDS.items() if spec.get("prunable"))
_HTTP_404 = re.compile(r"\bHTTP 404\b")


# --------------------------------------------------------------------------- #
#  Planning                                                                    #
# --------------------------------------------------------------------------- #

def deferred_note(stats):
    """The ", N off-mission board(s) deferred to Xh" clause for a plan()
    `stats` dict, and "" when this pass deferred none. run()'s header and
    harvest.py --list both print it; neither spells it out.

    >>> deferred_note({"offmission_skipped": 0})
    ''
    >>> deferred_note({"offmission_skipped": 2})    # doctest: +ELLIPSIS
    ', 2 off-mission board(s) deferred to ...h'
    """
    n = stats.get("offmission_skipped", 0)
    return (f", {n} off-mission board(s) deferred to "
            f"{config.HARVEST_OFFMISSION_HOURS:g}h") if n else ""


def _ats_rank(ats):
    """Cheap JSON boards first, then the heavyweights."""
    return 0 if ats in LIGHTWEIGHT else 1


def plan(conn, only=None, names=None, min_age_hours=None,
         limit=None, now=None, stats=None):
    """The boards this run will pull, in run order.

    `only` restricts to a set of ATS names, `names` to company names
    (case-insensitive); boards harvested within `min_age_hours` are skipped
    unless named explicitly. Cheapest ATSes first, then smaller boards
    before bigger ones, so an interrupted run has still banked the most
    boards per minute.

    A board that is config.is_offmission_inactive -- the one
    off-mission/inactive rule, shared with the whole-board page budget
    (config.board_max_pages, read by src.ats.fetchers.company) and
    defined in config.policy because ats sits BELOW crawl in the import
    DAG -- waits the longer config.HARVEST_OFFMISSION_HOURS instead of
    `min_age_hours`. Such a board is still fetched every pass, per the
    "harvest every board" mandate -- just not every `min_age_hours`.

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
    -- and `"min_age_hours"` set to the ordinary cutoff this call resolved
    the sentinel to. Both are what a caller's header line says (run()
    below, harvest.py --list); neither re-derives the default itself, so
    the sentinel rule has exactly one writer. The out-parameter shape is
    this module's own (see _hydrate_rows' `stats`): the return value is
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
    ['Old', 'Small', 'Big']
    >>> [c["name"] for c in plan(conn, names=["fresh"], now=now)]
    ['Fresh']
    >>> [c["name"] for c in plan(conn, only={"workday"}, now=now)]
    ['Big']

    An off-mission, inactive board waits the long interval instead of
    `min_age_hours` -- unless it is named explicitly, or the caller passes
    a `min_age_hours` other than the default:

    >>> _ = store.upsert_company(conn, {"name": "Stale", "ats": "lever",
    ...                                 "slug": "stale", "active": 0,
    ...                                 "mission_tier": "other"})
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

    Notes:
        The 2026-09-17 audit counted 289 off-mission inactive boards
        holding 51,134 stored jobs, against 288 active ones (any tier)
        holding 47,737 -- so roughly half of every whole-board pass was
        spent re-fetching rows triage's own mission gate discards on every
        single run.
    """
    now = now or datetime.now()
    # The sentinel, per the docstring: only an unset min_age_hours lets the
    # long interval apply at all.
    offmission_cutoff = None
    if min_age_hours is None:
        min_age_hours = MIN_AGE_HOURS
        offmission_cutoff = (
            now - timedelta(hours=config.HARVEST_OFFMISSION_HOURS)
        ).isoformat()
    cutoff = (now - timedelta(hours=min_age_hours)).isoformat()
    want = {n.strip().lower() for n in names or [] if n.strip()}
    rows = []
    offmission_skipped = 0
    for c in store.harvestable_companies(conn):
        if only and c.get("ats") not in only:
            continue
        if want:
            if (c.get("name") or "").lower() not in want:
                continue
        else:
            last = c.get("last_harvested_at") or ""
            board_cutoff = cutoff
            if offmission_cutoff is not None and config.is_offmission_inactive(c):
                board_cutoff = offmission_cutoff
                if cutoff >= last > offmission_cutoff:
                    offmission_skipped += 1
            if last > board_cutoff:
                continue
        rows.append(c)
    rows.sort(key=lambda c: (_ats_rank(c.get("ats")),
                             c.get("total_job_count") or 0,
                             (c.get("name") or "").lower()))
    if stats is not None:
        stats["offmission_skipped"] = offmission_skipped
        stats["min_age_hours"] = min_age_hours
    return rows[:limit] if limit else rows


# --------------------------------------------------------------------------- #
#  One board                                                                   #
# --------------------------------------------------------------------------- #

def _row(job, company, stamp):
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


def fetch_whole_board(company):
    """The company's full listing, unfiltered."""
    return company_fetch.fetch_company(company, None)


def _soft_failed(stats):
    """A board that answered with an error rather than an empty one: no
    rows, and its own fetch reported failures (net.http.snapshot_info).

    >>> _soft_failed({"fetched": 0, "fetch_errors": 2})
    True
    >>> _soft_failed({"fetched": 0, "fetch_errors": 0})
    False
    """
    return not stats.get("fetched") and bool(stats.get("fetch_errors"))


def bury_404_board(conn, company, error):
    """Mark `company` 'board-dead:<ats>' and deactivate it when `error` is
    an HTTP 404 from a DEFINITIVE_404_ATS listing; the caller has already
    seen the board fail once before. Returns the reason written, else None.

    Deactivated exactly as mark_harvested's promotion is, which is what
    lets reresolve_misses re-check it."""
    ats = company.get("ats")
    if ats not in DEFINITIVE_404_ATS or not _HTTP_404.search(error or ""):
        return None
    reason = f"board-dead:{ats}"
    store.deactivate_company(conn, company["id"])
    store.record_miss(conn, company["name"], reason)
    print(f"    [!] {company['name']} ({ats}): listing endpoint 404 twice - "
          f"board-dead, deactivated (reresolve re-checks it)")
    return reason


def harvest_board(company, db_path, progress=lambda: None, hydrate=False,
                  delay=None, now=None, backoff_s=MISS_BACKOFF_S):
    """Fetch, hydrate and store ONE board. Runs on a worker thread and opens
    its own connection (sqlite connections are per-thread). Returns a stats
    dict; a fetch failure is reported, not raised, and leaves the store
    untouched (nothing is closed on a failed or empty snapshot; see
    _store_board for an incomplete or capped one).

    `progress()` is called after the listing returns and after every
    hydrated row, which is how the run's watchdog tells a slow board from a
    wedged one. `delay` defaults to config.HYDRATE_DELAY_S.

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
    stats = {"name": company.get("name"), "ats": company.get("ats"),
             "fetched": 0, "new": 0, "hydrated": 0, "unhydrated": 0,
             "closed": 0, "reopened": 0, "fetch_errors": 0, "err": None,
             "incomplete": False, "capped": False, "capped_total": None,
             "secs": 0.0}
    # A fetcher does not RAISE on a dead board -- it reports and returns [],
    # which is also what a board with nothing on it returns, so `fetched: 0`
    # alone cannot tell "404" from "no openings". net.http counts the
    # reported failures per thread, and one board owns one thread here, so
    # resetting around the fetch attributes them to exactly this board.
    http.reset_fetch_failures()
    try:
        jobs = fetch_whole_board(company) or []
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
        _store_board(db_path, jobs, company, stats, progress,
                     hydrate, delay, backoff_s, now)
    except Exception as e:                      # noqa: BLE001 - reported
        # A store failure is NOT a fetch error, and calling it one hid this
        # bug for a day: every "database is locked" reads as an unreachable
        # board in the 2026-09-10 logs. The board's postings are simply not
        # written; the next pass re-fetches them.
        stats["err"] = f"store: {type(e).__name__}: {e}"
    stats["secs"] = time.monotonic() - t0
    return stats


def _store_board(db_path, jobs, company, stats, progress, hydrate, delay,
                 backoff_s, now):
    """Hydrate (optionally) and write one board's snapshot. The write itself
    is ONE transaction (store.batch), which is the whole point: a board is
    one lock acquisition, not one per posting.

    The rows that arrived are always stored. Closing against the snapshot
    is what a partial pull cannot be trusted for: an INCOMPLETE one (a
    fetch failed partway) closes nothing, and neither does a CAPPED one
    (store.sync_job_statuses's `capped`) -- its vanished rows wait for
    ops.check_closed_jobs's URL probe instead.

    A soft-failed snapshot (_soft_failed) is recorded as one:
    store.mark_harvested keeps the last good total_job_count and runs its
    dead-board cycle."""
    conn = store.connect(db_path)
    try:
        # Bodies already in the store (an earlier harvest, or a crawl) are
        # reused, never re-fetched: many listings come back bodiless every
        # time, and re-hydrating 100 known rows is what tripped one host's
        # limit on a second run (2026-09-10).
        if jobs and company.get("id"):
            stored = store.descriptions_for_company(conn, company["id"])
            for j in jobs:
                if not j.get("description") and j["id"] in stored:
                    j["description"] = stored[j["id"]]
        if hydrate:
            _hydrate_rows(jobs, company, stats, progress, delay, backoff_s)
        stamp_dt = now or datetime.now()
        stamp = stamp_dt.isoformat()
        with store.batch(conn):
            if jobs and company.get("id") and not stats.get("incomplete"):
                # A full, successful snapshot is the best evidence there is
                # for what the board lists: close what vanished, revive
                # returners -- across every track (track=None), on this
                # pass's own stamp.
                re_, cl = store.sync_job_statuses(
                    conn, company["id"], jobs, track=None,
                    capped=stats.get("capped", False), now=stamp_dt)
                stats["reopened"], stats["closed"] = re_, cl
            for j in jobs:
                # A whole-board listing can say "N Locations" every pass;
                # the real list triage resolved stays.
                if store.upsert_job(
                        conn, _row(j, company, stamp),
                        keep_location=location_unknown(j.get("location"))):
                    stats["new"] += 1
            promoted = None
            if company.get("id"):
                promoted = store.mark_harvested(
                    conn, company["id"], len(jobs),
                    soft_fail=_soft_failed(stats), now=stamp_dt)
                # `company` is the pre-pass row: a fetch-error miss already
                # on it means this is the second failing pass in a row.
                if (not promoted and _soft_failed(stats)
                        and company.get("miss_reason") == "fetch-error:harvest"):
                    bury_404_board(conn, company, stats.get("last_error"))
        if promoted:
            print(f"    [!] {company.get('name')}: no jobs for >= "
                  f"{store.HARVEST_DEAD_AFTER_DAYS}d since its first fetch "
                  f"error - promoted to '{promoted}'")
    finally:
        conn.close()


def _hydrate_rows(jobs, company, stats, progress, delay, backoff_s):
    """Resolve every row in `jobs` that still needs a detail call
    (company_fetch.needs_detail: no body yet, or a body already but a
    location the listing never resolved), in place, within the host's
    tolerances: config.HYDRATE_CAP_PER_RUN rows, `delay` between GETs
    (config.HYDRATE_DELAY_S when None), and the miss-streak breaker.

    Fills stats['hydrated'] (rows whose detail need was resolved this
    pass -- a body arrived, or a location-only row's location did) and
    stats['unhydrated'] (rows that still need one when this pass ends,
    whether never reached or tried and failed). A location lookup that
    comes back empty counts as a miss for the breaker exactly like a
    failed body fetch -- needs_detail decides "resolved or not" either
    way, so the two cases share one counter.
    """
    todo = [j for j in jobs if company_fetch.needs_detail(j)]
    cap = config.HYDRATE_CAP_PER_RUN
    delay = config.HYDRATE_DELAY_S if delay is None else delay
    if len(todo) > cap:
        print(f"    {company.get('name')}: {len(todo)} row(s) needing detail, "
              f"cap is {cap}/run - the rest next run")
        todo = todo[:cap]
    streak = paused = 0
    try:
        for i, j in enumerate(todo):
            _log.debug("hydrate %s", j.get("url"))
            j["_tried"] = True          # attempted (vs. left over the cap)
            try:
                company_fetch.hydrate_description(j, company)
            except Exception as e:              # noqa: BLE001 - per row
                _log.debug("hydrate %s failed: %s", j.get("url"), e)
            progress()
            if not company_fetch.needs_detail(j):
                stats["hydrated"] += 1
                streak = 0
            else:
                streak += 1
            if streak >= MISS_STREAK:
                if paused:
                    left = len(todo) - i - 1
                    print(f"    [!] {company.get('name')}: {streak} more "
                          f"misses after a pause - {left} row(s) left "
                          f"unresolved for the next run")
                    break
                paused += 1
                print(f"    [!] {company.get('name')}: {streak} hydration "
                      f"misses in a row - pausing {backoff_s:.0f}s")
                streak = 0
                time.sleep(backoff_s)
                progress()
            elif delay:
                time.sleep(delay)
    finally:
        stats["unhydrated"] = sum(1 for j in jobs if company_fetch.needs_detail(j))


# --------------------------------------------------------------------------- #
#  The run                                                                     #
# --------------------------------------------------------------------------- #

def run(db_path=None, only=None, names=None, min_age_hours=None,
        limit=None, max_workers=DEFAULT_WORKERS, hydrate=False,
        max_hours=None, stall_s=STALL_S, poll_s=30.0,
        board_fn=harvest_board, triage=True, score_cap=None):
    """Harvest every planned board, then triage what was stored and
    rewrite every roster track's digest. Returns the summary dict (also
    printed); the triage summary rides in it under "triage".

    `triage=False` skips the gate/hydrate/score pass (the rows wait for
    the next one, or for `run_scraper.py --triage`); `score_cap` bounds
    that pass's Claude fit calls (triage.SCORE_CAP when None). `poll_s` is
    how often the watchdog looks at the in-flight boards; `board_fn`
    exists for tests (it is harvest_board's signature).

    The header line names how many off-mission, inactive boards `plan`
    deferred to its longer interval this pass (plan's `stats` output;
    silent when there are none), so a run that looks small is never a
    silent drop -- it says which boards it left for later and why.
    """
    db_path = db_path or config.STORE_DB_PATH
    claude_baseline = cache_stats()     # this pass's own Claude spend footer
    with closing(store.connect(db_path)) as conn:
        plan_stats = {}
        boards = plan(conn, only=only, names=names, min_age_hours=min_age_hours,
                      limit=limit, stats=plan_stats)
    age = plan_stats["min_age_hours"]

    bar = "=" * 70
    print(f"\n{bar}\n  [HARVEST] whole-board pull - {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  {len(boards)} board(s), {max_workers} at a time, "
          f"hydrate={'on' if hydrate else 'off'}, "
          f"skip if harvested < {age:g}h ago" + deferred_note(plan_stats)
          + (f", stop after {max_hours:g}h" if max_hours else "") + f"\n{bar}\n")

    summary = {"boards": len(boards), "ok": 0, "err": 0, "stalled": 0,
               "dead": 0, "fetched": 0, "new": 0, "hydrated": 0, "closed": 0,
               "reopened": 0, "secs": 0.0}
    if not boards:
        print("  nothing to do")
        if triage:
            summary["triage"] = _triage(db_path, max_workers, score_cap,
                                        claude_baseline)
        return summary

    t_start = time.monotonic()
    deadline = t_start + max_hours * 3600 if max_hours else None
    last_progress = {}                  # company id -> monotonic seconds
    lock = threading.Lock()

    def _tick(cid):
        def progress():
            with lock:
                last_progress[cid] = time.monotonic()
        return progress

    done_n = [0]

    def _report(c, s):
        done_n[0] += 1
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
        print(f"  {prefix}[{done_n[0]:>3}/{len(boards)}] {c['name']} "
              f"({c['ats']}): {status}")
        # Churn a board's own board-diff should have damped, still showing
        # up: worth a human's attention, not just a debug line.
        fetched = s.get("fetched") or 0
        reopened = s.get("reopened") or 0
        if reopened > max(10, 0.05 * fetched):
            print(f"    [!] {c['name']} ({c['ats']}): {reopened} reopened "
                  f"of {fetched} fetched - board churning")
        _log.debug("board %s stats %s", c.get("name"), s)

    ex = ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix="harvest")
    futs = {}
    for c in boards:
        cid = c.get("id") or c.get("name")
        futs[ex.submit(board_fn, c, db_path, progress=_tick(cid),
                       hydrate=hydrate)] = c
    pending = set(futs)
    while pending:
        done, pending = fut_wait(pending, timeout=poll_s,
                                 return_when=FIRST_COMPLETED)
        for fut in done:
            c = futs[fut]
            try:
                s = fut.result()
            except Exception as e:              # noqa: BLE001 - reported
                s = {"err": f"{type(e).__name__}: {e}", "fetched": 0,
                     "new": 0, "hydrated": 0, "closed": 0, "reopened": 0,
                     "secs": 0.0}
            _report(c, s)
            if s["err"]:
                summary["err"] += 1
            else:
                summary["ok"] += 1
                if _soft_failed(s):
                    summary["dead"] += 1
            for k in ("fetched", "new", "hydrated", "closed", "reopened"):
                summary[k] += s[k]
        now = time.monotonic()
        out_of_time = deadline is not None and now >= deadline
        for fut in list(pending):
            c = futs[fut]
            cid = c.get("id") or c.get("name")
            if not fut.running():
                if out_of_time and fut.cancel():
                    pending.discard(fut)
                continue
            with lock:
                started = last_progress.setdefault(cid, now)
            if out_of_time or now - started > stall_s:
                why = ("run out of time" if out_of_time
                       else f"no progress in {stall_s:.0f}s")
                print(f"  [!] {c['name']} ({c['ats']}): {why} - abandoned")
                summary["stalled"] += 1
                pending.discard(fut)
    # Abandoned boards keep their thread until the process exits; never
    # join them, or one wedged board holds the whole run hostage.
    ex.shutdown(wait=False, cancel_futures=True)

    summary["secs"] = time.monotonic() - t_start
    skipped = summary["boards"] - summary["ok"] - summary["err"] \
        - summary["stalled"]
    print(f"\n{bar}\n  HARVEST SUMMARY")
    print(f"  boards: {summary['ok']} ok, {summary['err']} failed, "
          f"{summary['stalled']} abandoned"
          + (f", {summary['dead']} answered with an error and no jobs"
             if summary["dead"] else "")
          + (f", {skipped} not started" if skipped > 0 else ""))
    print(f"  jobs:   {summary['fetched']} fetched, {summary['new']} new, "
          f"{summary['hydrated']} hydrated, {summary['closed']} closed, "
          f"{summary['reopened']} reopened")
    # Roster hygiene: a board still named after its own slug/tenant
    # fetches fine, so nothing else in the log names it for renaming.
    unnamed = sorted((c for c in boards if slug_named(c)),
                     key=lambda c: -(c.get("total_job_count") or 0))
    print(f"  {len(unnamed)} board(s) still named after their own "
          f"slug/tenant"
          + (f", largest first: {', '.join(c['name'] for c in unnamed[:10])}"
             + (", ..." if len(unnamed) > 10 else "") if unnamed else ""))
    print(f"  time:   {summary['secs'] / 60:.1f} min\n{bar}")
    if triage:
        summary["triage"] = _triage(db_path, max_workers, score_cap,
                                    claude_baseline)
    return summary


def _triage(db_path, max_workers, score_cap, claude_baseline):
    """The pass's second half; returns triage.run's summary.

    The gate/hydrate/score pass over everything pending in the store --
    this pass's rows and any earlier pass left waiting on a body or over
    the scoring cap -- then, on that same `db_path`: each roster track's
    deep verify (ops.verify_top, within its verify_top), one closed-URL
    probe (ops.check_closed_jobs: CLOSED_PROBE_LIMIT open rows no board
    has vouched for in CLOSED_PROBE_STALE_DAYS), each roster track's
    digest (no email), and the Claude spend footer for the calls made
    since `claude_baseline`. With no API key, or the breaker already
    tripped, the verify step is one printed line instead.

    Notes:
        triage is imported here because it imports this module. The steps
        after it use `db_path`, not maintenance.track_store(t): that opens
        each track's configured store, a different file whenever `db_path`
        is overridden (tests, `harvest.py --db`), and triage.run reads
        every roster track from the one `db_path` too. The verify guard
        sits here so a dead API prints one line, not one per track.
    """
    from src.crawl import triage
    kw = {"score_cap": score_cap} if score_cap is not None else {}
    result = triage.run(db_path=db_path, max_workers=max_workers, **kw)
    conn = store.connect(db_path)
    try:
        tracks = triage.roster_tracks()
        down = api_disabled()
        if not have_api_key() or down:
            print(f"\n  [!] verify skipped for {len(tracks)} roster track(s): "
                  f"{down or 'no ANTHROPIC_API_KEY configured'}")
        else:
            for t in tracks:
                if t.get("verify_top"):
                    verify_top(top_n=t["verify_top"],
                              max_workers=max(2, max_workers // 2),
                              conn=conn, t=t)
        check_closed_jobs(max_workers=max_workers, limit=CLOSED_PROBE_LIMIT,
                          stale_days=CLOSED_PROBE_STALE_DAYS, conn=conn)
        for t in tracks:
            rewrite_digest(conn, t, top_n=5,
                           heading=f"\n  [{t['track']}] digest rewritten:")
    finally:
        conn.close()
    report_cache_stats(claude_baseline)
    return result
