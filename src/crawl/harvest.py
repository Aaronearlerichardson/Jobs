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
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as fut_wait
from datetime import datetime, timedelta

from src import config
from src import store
from src.match.locality import geo_mode

from src.ats.fetchers import company as company_fetch
from src.ats.registry import ATS_REGISTRY, LIGHTWEIGHT
from src.net.util import worker_count

_log = logging.getLogger(__name__)

# Boards in flight at once. Each board is one host, and a board's own
# requests are serial, so this is also the per-host politeness bound.
DEFAULT_WORKERS = worker_count("HARVEST_WORKERS")
# Pause between one board's detail GETs (hydration is a page per posting)
# when the ATS registry has no politeness pause for the family. The
# registry's own value wins when it has one: Workday cut Insulet off after
# 151 detail GETs at two per second (2026-09-10 smoke run), and its
# registry pause is a full second.
HYDRATE_DELAY_S = 0.5
# Consecutive hydration misses that mean the host has stopped answering
# (Workday drops the connection outright once it decides you are a bot).
# The first streak earns one pause-and-retry; a second ends hydration for
# this board, and the next run picks the bodiless rows up again.
MISS_STREAK = 5
MISS_BACKOFF_S = 90.0
# Per-board, per-run ceiling on detail GETs for hosts that throttle by
# volume. Workday closed the connection after 151 detail GETs (2026-09-10,
# Insulet), and again after 42 on a retry ten minutes later, and the first
# refused request hung for minutes each time. Staying under the limit
# avoids both; the rows left bodiless are picked up by later runs (the
# store's existing bodies are never re-fetched, so each run advances).
HYDRATE_CAP = {"workday": 100}
# A board that has made NO progress (no fetch return, no hydrated row) for
# this long is abandoned. Generous: one detail GET is bounded by
# config.FETCH_TIMEOUT, so only a wedged fetcher gets here.
STALL_S = 900.0
# A board harvested more recently than this is skipped, which is what makes
# a restarted (or Task-Scheduler-repeated) run resume where it left off.
MIN_AGE_HOURS = 6.0


# --------------------------------------------------------------------------- #
#  Planning                                                                    #
# --------------------------------------------------------------------------- #

def _ats_rank(ats):
    """Cheap JSON boards first, then the heavyweights."""
    return 0 if ats in LIGHTWEIGHT else 1


def plan(conn, only=None, names=None, min_age_hours=MIN_AGE_HOURS,
         limit=None, now=None):
    """The boards this run will pull, in run order.

    `only` restricts to a set of ATS names, `names` to company names
    (case-insensitive); boards harvested within `min_age_hours` are skipped
    unless named explicitly. Cheapest ATSes first, then smaller boards
    before bigger ones, so an interrupted run has still banked the most
    boards per minute.

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
    """
    now = now or datetime.now()
    cutoff = (now - timedelta(hours=min_age_hours)).isoformat()
    want = {n.strip().lower() for n in names or [] if n.strip()}
    rows = []
    for c in store.harvestable_companies(conn):
        if only and c.get("ats") not in only:
            continue
        if want:
            if (c.get("name") or "").lower() not in want:
                continue
        elif (c.get("last_harvested_at") or "") > cutoff:
            continue
        rows.append(c)
    rows.sort(key=lambda c: (_ats_rank(c.get("ats")),
                             c.get("total_job_count") or 0,
                             (c.get("name") or "").lower()))
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
    """The company's full listing, unfiltered. iCIMS is the one fetcher whose
    default narrows a board even with no location regex (it sends the
    locality as the search term), so it is called with that off."""
    if company.get("ats") == "icims":
        return company_fetch.fetch_icims_all(company["slug"], None,
                                             search_location=None)
    return company_fetch.fetch_company(company, None)


def hydrate_delay(ats):
    """Seconds between one board's detail GETs: the ATS registry's
    politeness pause when it has one, else HYDRATE_DELAY_S.

    >>> hydrate_delay("workday"), hydrate_delay("custom")
    (1.0, 0.5)
    """
    entry = ATS_REGISTRY.get(ats)
    return max(entry[2], HYDRATE_DELAY_S) if entry else HYDRATE_DELAY_S


def harvest_board(company, db_path, progress=lambda: None, hydrate=False,
                  delay=None, now=None, backoff_s=MISS_BACKOFF_S):
    """Fetch, hydrate and store ONE board. Runs on a worker thread and opens
    its own connection (sqlite connections are per-thread). Returns a stats
    dict; a fetch failure is reported, not raised, and leaves the store
    untouched (nothing is closed on a failed or empty snapshot).

    `progress()` is called after the listing returns and after every
    hydrated row, which is how the run's watchdog tells a slow board from a
    wedged one. `delay` defaults to hydrate_delay(ats).

    `hydrate` is OFF by default: the listing is stored bodiless and the
    triage pass (src/crawl/triage.py) fetches bodies for the rows that
    survive its free gates. Turning it on hydrates the whole board here
    (the pre-triage behaviour), which spends the host's detail budget on
    rows a title check would have dropped.
    """
    t0 = time.monotonic()
    if delay is None:
        delay = hydrate_delay(company.get("ats"))
    stats = {"name": company.get("name"), "ats": company.get("ats"),
             "fetched": 0, "new": 0, "hydrated": 0, "unhydrated": 0,
             "closed": 0, "reopened": 0, "err": None, "secs": 0.0}
    try:
        jobs = fetch_whole_board(company) or []
    except Exception as e:                      # noqa: BLE001 - reported
        stats["err"] = f"fetch: {type(e).__name__}: {e}"
        stats["secs"] = time.monotonic() - t0
        return stats
    progress()
    stats["fetched"] = len(jobs)

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
    one lock acquisition, not one per posting."""
    conn = store.connect(db_path)
    try:
        # Bodies already in the store (an earlier harvest, or a crawl) are
        # reused, never re-fetched: the listing comes back bodiless from
        # Workday every time, and re-hydrating 100 known rows is what
        # tripped the host's limit on the second Insulet run.
        if jobs and company.get("id"):
            stored = store.descriptions_for_company(conn, company["id"])
            for j in jobs:
                if not j.get("description") and j["id"] in stored:
                    j["description"] = stored[j["id"]]
        if hydrate:
            _hydrate_rows(jobs, company, stats, progress, delay, backoff_s)
        stamp = (now or datetime.now()).isoformat()
        with store.batch(conn):
            if jobs and company.get("id"):
                # A full, successful snapshot is the best evidence there is
                # for what the board lists: close what vanished, revive
                # returners -- across every track (track=None).
                re_, cl = store.sync_job_statuses(conn, company["id"], jobs,
                                                  track=None)
                stats["reopened"], stats["closed"] = re_, cl
            for j in jobs:
                if store.upsert_job(conn, _row(j, company, stamp)):
                    stats["new"] += 1
            if company.get("id"):
                store.mark_harvested(conn, company["id"], len(jobs))
    finally:
        conn.close()


def _hydrate_rows(jobs, company, stats, progress, delay, backoff_s):
    """Fetch the body of every bodiless row in `jobs`, in place, within the
    host's tolerances: the per-run cap, the pause between GETs, and the
    miss-streak breaker. Fills stats['hydrated'] / stats['unhydrated']."""
    todo = [j for j in jobs if not j.get("description")]
    cap = HYDRATE_CAP.get(company.get("ats"))
    if cap and len(todo) > cap:
        print(f"    {company.get('name')}: {len(todo)} bodiless row(s), "
              f"{company.get('ats')} cap is {cap}/run - the rest next run")
        todo = todo[:cap]
    streak = paused = 0
    try:
        for i, j in enumerate(todo):
            _log.debug("hydrate %s", j.get("url"))
            j["_tried"] = True          # attempted (vs. left over the cap)
            try:
                company_fetch.hydrate_description(j)
            except Exception as e:              # noqa: BLE001 - per row
                _log.debug("hydrate %s failed: %s", j.get("url"), e)
            progress()
            if j.get("description"):
                stats["hydrated"] += 1
                streak = 0
            else:
                streak += 1
            if streak >= MISS_STREAK:
                if paused:
                    left = len(todo) - i - 1
                    print(f"    [!] {company.get('name')}: {streak} more "
                          f"misses after a pause - {left} row(s) left "
                          f"bodiless for the next run")
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
        stats["unhydrated"] = sum(1 for j in jobs if not j.get("description"))


# --------------------------------------------------------------------------- #
#  The run                                                                     #
# --------------------------------------------------------------------------- #

def run(db_path=None, only=None, names=None, min_age_hours=MIN_AGE_HOURS,
        limit=None, max_workers=DEFAULT_WORKERS, hydrate=False,
        max_hours=None, stall_s=STALL_S, poll_s=30.0,
        board_fn=harvest_board, triage=True, score_cap=None):
    """Harvest every planned board, then triage what was stored. Returns
    the summary dict (also printed); the triage summary rides in it under
    "triage".

    `triage=False` skips the gate/hydrate/score pass (the rows wait for
    the next one, or for `run_scraper.py --triage`); `score_cap` bounds
    that pass's Claude fit calls (triage.SCORE_CAP when None). `poll_s` is
    how often the watchdog looks at the in-flight boards; `board_fn`
    exists for tests (it is harvest_board's signature).
    """
    db_path = db_path or config.STORE_DB_PATH
    conn = store.connect(db_path)
    boards = plan(conn, only=only, names=names, min_age_hours=min_age_hours,
                  limit=limit)
    conn.close()

    bar = "=" * 70
    print(f"\n{bar}\n  [HARVEST] whole-board pull - {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  {len(boards)} board(s), {max_workers} at a time, "
          f"hydrate={'on' if hydrate else 'off'}, "
          f"skip if harvested < {min_age_hours:g}h ago"
          + (f", stop after {max_hours:g}h" if max_hours else "") + f"\n{bar}\n")

    summary = {"boards": len(boards), "ok": 0, "err": 0, "stalled": 0,
               "fetched": 0, "new": 0, "hydrated": 0, "closed": 0,
               "reopened": 0, "secs": 0.0}
    if not boards:
        print("  nothing to do")
        if triage:
            summary["triage"] = _triage(db_path, max_workers, score_cap)
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
        if s["err"]:
            status = s["err"]
        else:
            status = (f"{s['fetched']} job(s), {s['new']} new, "
                      f"{s['hydrated']} hydrated"
                      + (f" ({s['unhydrated']} bodiless)"
                         if s.get("unhydrated") else "")
                      + f", {s['closed']} closed, {s['secs']:.0f}s")
        print(f"  [{done_n[0]:>3}/{len(boards)}] {c['name']} ({c['ats']}): "
              f"{status}")
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
          + (f", {skipped} not started" if skipped > 0 else ""))
    print(f"  jobs:   {summary['fetched']} fetched, {summary['new']} new, "
          f"{summary['hydrated']} hydrated, {summary['closed']} closed, "
          f"{summary['reopened']} reopened")
    print(f"  time:   {summary['secs'] / 60:.1f} min\n{bar}")
    if triage:
        summary["triage"] = _triage(db_path, max_workers, score_cap)
    return summary


def _triage(db_path, max_workers, score_cap):
    """The gate/hydrate/score pass over everything pending in the store --
    this pass's rows and any earlier pass left waiting on a body or over
    the scoring cap. Imported here: triage imports this module."""
    from src.crawl import triage
    kw = {"score_cap": score_cap} if score_cap is not None else {}
    return triage.run(db_path=db_path, max_workers=max_workers, **kw)
