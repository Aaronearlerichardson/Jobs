#!/usr/bin/env python3
"""Background whole-board harvester (see src/crawl/harvest.py).

    python harvest.py                       # a pass now, then one every 12 h
    python harvest.py --every 8             # ... every 8 h instead
    python harvest.py --once                # one pass, then exit
    python harvest.py --list                # print the plan, fetch nothing
    python harvest.py --only greenhouse,lever --limit 5 --once
    python harvest.py --names "NVIDIA" "IQVIA" --once   # named boards, even if fresh
    python harvest.py --max-hours 6         # abandon a pass still running after 6 h
    python harvest.py --once --no-triage    # store listings only; gate/score later
    python harvest.py --once --score-cap 50 # at most 50 fit calls this pass

Meant to sit in the background for the whole session: put a shortcut to
JobHarvester.exe in the Startup folder (Win+R, `shell:startup`) and it
starts at log-on, runs a pass, and parks on a timed wait until the next
one. It keeps a console window of its own, like the crawler UI -- that
window is how you see what it is doing and how you stop it (close it, or
Ctrl+C). Minimise it if it is in the way; it was built windowless for a
while and the only thing that achieved was making Task Manager the off
switch. Parked, it costs no CPU at all -- the thread is not scheduled until
its deadline -- and the deadline is wall-clock, so a laptop that slept
through it runs the pass as soon as it wakes. That "as soon as it wakes"
is doing the real work, and it is a limit worth knowing: this is an
in-process timer, and no process runs while Windows itself is asleep, so
`--every` cannot WAKE the laptop -- it only notices, once something else
wakes it, that the deadline has already passed (a pass that starts more
than 15 minutes late says so; see OVERDUE_THRESHOLD_S). If a pass has to
happen on time even through sleep, replace `--every` with a Windows Task
Scheduler trigger with "Wake the computer to run this task" checked, and
have it invoke `harvest.py --once` on the schedule instead -- the
scheduler can wake the machine; this script cannot. Each pass pulls every
board with a fetchable ATS that has not been harvested in the last
--min-age-hours and stores every posting unscored, then runs the triage
pass (src/crawl/triage.py): the crawl's gates cheapest-first, bodies only
for survivors, one Claude fit call only for each hydrated survivor, capped
per pass. A second copy started while one is running exits at once (lock
file in the data directory). Each pass gets its own session log
(data/logs/session-*-harvest.log).
"""

import argparse
import os
import sys
import threading
import time
import traceback
from contextlib import closing
from datetime import datetime

try:  # Windows consoles default to cp1252; job text carries em-dashes etc.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_EVERY_HOURS = 12.0
# Longest single wait between deadline checks. Short enough that a machine
# waking from a long sleep notices an overdue pass within minutes; a timed
# wait costs nothing while it lasts, so the chunking is free.
WAIT_CHUNK_S = 300.0
# How far a pass may start after its scheduled time before it is worth a
# [!] warning. Below this is ordinary jitter (another process briefly held
# the store lock); above it is a laptop that slept through one or more
# ticks -- the slips actually observed in the field ran 2 h to 17 h.
OVERDUE_THRESHOLD_S = 15 * 60


def next_pass_at(started, every_hours):
    """The epoch time of the pass after one that started at `started`:
    `every_hours` after its START, never after its end. run_forever waits
    for it and main() prints it, so the promise and the wait agree.

    >>> next_pass_at(0.0, 12) / 3600
    12.0
    """
    return started + every_hours * 3600


def overdue_warning(scheduled, started, threshold_s=OVERDUE_THRESHOLD_S):
    """The `[!]` line for a pass that started more than `threshold_s` after
    it was `scheduled`, or None. The first pass of a run (`scheduled` None)
    is never late.

    >>> overdue_warning(None, 100.0)
    >>> overdue_warning(1000.0, 1000.0 + 10 * 60)           # 10 min: too soon to warn
    >>> overdue_warning(1000.0, 1000.0 + OVERDUE_THRESHOLD_S)   # at the threshold
    >>> overdue_warning(1000.0, 900.0)                          # early
    >>> overdue_warning(1000.0, 1000.0 + 20 * 60).startswith(
    ...     "[!] pass is 20 min late (scheduled ")
    True

    Lateness reads in whole minutes under an hour and tenths of an hour
    from one hour up:

    >>> overdue_warning(0.0, 90 * 60).split(" (")[0]
    '[!] pass is 1.5 h late'
    >>> overdue_warning(0.0, 17 * 3600 + 20 * 60).split(" (")[0]
    '[!] pass is 17.3 h late'
    """
    if scheduled is None or started - scheduled <= threshold_s:
        return None
    late = started - scheduled
    how = f"{late / 3600:.1f} h" if late >= 3600 else f"{round(late / 60)} min"
    when = datetime.fromtimestamp(scheduled)
    return f"[!] pass is {how} late (scheduled {when:%Y-%m-%d %H:%M})"


def acquire_lock(path=None):
    """Hold an OS-level exclusive lock on `path` (default
    <DATA_DIR>/harvest.lock) for the life of the process (released by the
    OS on any exit, so a crash never leaves a stale lock). Returns the open
    handle, or None when another harvester already holds it."""
    if path is None:
        # config loads here, not at import, so a bad profile.toml or env
        # var raises inside main()'s "Press Enter" guard.
        from src import config
        path = config.DATA_DIR / "harvest.lock"
    fh = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def run_forever(pass_fn, every_hours, wait=None, clock=time.time,
                chunk_s=WAIT_CHUNK_S):
    """Run `pass_fn` now and then once every `every_hours`, measured via
    `next_pass_at` from the START of the previous pass (a pass that takes
    three hours does not push the schedule back). Never returns unless
    `wait` asks it to.

    `pass_fn(scheduled)` gets the epoch time its pass was due (None for the
    first, immediate one), so it can say how late it is (overdue_warning).

    `wait(seconds)` parks the thread; it returns True to stop the loop
    (threading.Event.wait semantics -- the default Event is never set, so
    the default wait only ever times out). `clock` and `chunk_s` exist for
    tests.

    >>> ticks, log = [0.0], []
    >>> def clock():
    ...     return ticks[0]
    >>> def wait(s):
    ...     ticks[0] += s
    ...     return False
    >>> def one_pass(scheduled):
    ...     log.append((clock(), scheduled))
    ...     ticks[0] += 3600          # the pass itself takes an hour
    ...     if len(log) == 3:
    ...         raise KeyboardInterrupt
    >>> try:
    ...     run_forever(one_pass, 12, wait=wait, clock=clock)
    ... except KeyboardInterrupt:
    ...     pass
    >>> [(started / 3600, sched and sched / 3600) for started, sched in log]
    [(0.0, None), (12.0, 12.0), (24.0, 24.0)]

    A pass that overruns its interval is followed at once, late, and the
    one after it is due `every_hours` after that late start:

    >>> ticks[0], log[:] = 0.0, []
    >>> def overrun(scheduled):
    ...     log.append((clock(), scheduled))
    ...     ticks[0] += 3600 * (20 if len(log) == 1 else 1)
    ...     if len(log) == 3:
    ...         raise KeyboardInterrupt
    >>> try:
    ...     run_forever(overrun, 12, wait=wait, clock=clock)
    ... except KeyboardInterrupt:
    ...     pass
    >>> [(started / 3600, sched and sched / 3600) for started, sched in log]
    [(0.0, None), (20.0, 12.0), (32.0, 32.0)]
    """
    wait = wait or threading.Event().wait
    scheduled = None
    while True:
        started = clock()
        pass_fn(scheduled)
        scheduled = next_pass_at(started, every_hours)
        while True:
            remaining = scheduled - clock()
            if remaining <= 0:
                break
            if wait(min(remaining, chunk_s)):
                return


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        description="Pull every board whole and store it unscored, on a "
                    "timer")
    ap.add_argument("--every", type=float, default=DEFAULT_EVERY_HOURS,
                    metavar="HOURS",
                    help=f"Hours between passes (default {DEFAULT_EVERY_HOURS:g})")
    ap.add_argument("--once", action="store_true",
                    help="One pass, then exit (no timer)")
    ap.add_argument("--list", action="store_true",
                    help="Print the boards a pass would pull, then exit")
    ap.add_argument("--only", metavar="ATS[,ATS]",
                    help="Only these ATS families (e.g. greenhouse,lever)")
    ap.add_argument("--names", nargs="+", metavar="NAME",
                    help="Only these companies (pulled even if fresh)")
    ap.add_argument("--limit", type=int, help="At most this many boards")
    ap.add_argument("--min-age-hours", type=float, default=None,
                    help="Skip boards harvested more recently than this "
                         "(default 6, or [policy] "
                         "harvest_offmission_hours for an off-mission, "
                         "inactive board). Passing this applies ONE "
                         "cutoff to every board, so 0 means harvest "
                         "everything now")
    ap.add_argument("--max-hours", type=float,
                    help="Abandon whatever is still running after this long")
    ap.add_argument("--workers", type=int, default=None,
                    help="Boards in flight at once (default: n_cpus-1, "
                         "or HARVEST_WORKERS)")
    ap.add_argument("--hydrate", action="store_true",
                    help="Fetch every posting's description during the pull "
                         "(default: triage fetches only the rows that pass "
                         "its free gates)")
    ap.add_argument("--no-triage", action="store_true",
                    help="Skip the gate/hydrate/score pass after the pull "
                         "(run it later with run_scraper.py --triage)")
    ap.add_argument("--score-cap", type=int, default=None, metavar="N",
                    help="Claude fit calls per pass (default 300, "
                         "src.crawl.triage.SCORE_CAP)")
    ap.add_argument("--db", help="Store path (default: the data dir's jobs.db)")
    args = ap.parse_args(argv)

    from src import session_log
    from src.crawl import harvest

    only = ({s.strip() for s in args.only.split(",") if s.strip()}
            if args.only else None)
    # None (no flag) leaves both intervals to harvest.plan; a value the
    # user typed overrides them uniformly. See plan()'s docstring.
    min_age = args.min_age_hours

    if args.list:
        from src import store
        with closing(store.connect(args.db)) as conn:
            plan_stats = {}
            boards = harvest.plan(conn, only=only, names=args.names,
                                  min_age_hours=min_age, limit=args.limit,
                                  stats=plan_stats)
        for c in boards:
            print(f"  {c['name']}  ({c['ats']}, ~{c.get('total_job_count') or 0}"
                  f" jobs, last harvested {c.get('last_harvested_at') or 'never'})")
        print(f"  {len(boards)} board(s)"
              + harvest.deferred_note(plan_stats))
        return 0

    lock = acquire_lock()
    if lock is None:
        print("  [!] another harvester holds the lock; exiting")
        return 0

    stalled = [0]

    def one_pass(scheduled=None):
        # Captured before session_log.start() so a slow log-file open (or
        # the [!] print it enables below) is never counted as lateness.
        started = time.time()
        # A fresh session log per pass, so each shows up in data/logs on
        # its own and retention pruning treats it like any other run.
        session_log.start(["--harvest", *argv])
        try:
            warning = overdue_warning(scheduled, started)
            if warning:
                print(f"  {warning}")
            try:
                summary = harvest.run(
                    db_path=args.db, only=only, names=args.names,
                    min_age_hours=min_age, limit=args.limit,
                    max_workers=args.workers or harvest.DEFAULT_WORKERS,
                    hydrate=args.hydrate, max_hours=args.max_hours,
                    triage=not args.no_triage, score_cap=args.score_cap)
                stalled[0] += summary["stalled"]
            except Exception:
                # Put the traceback in the session log while it is still
                # open: the console shows it too, but the window scrolls and
                # the log is what you still have tomorrow.
                # A pass that dies must not take the timer down with it;
                # --once is the interactive case and should fail loudly.
                print("\n  [!] harvest pass failed:", file=sys.stderr)
                traceback.print_exc()
                if args.once:
                    raise
            if not args.once:
                # Same START-of-this-pass anchor run_forever schedules the
                # next call from, so the promise and the wait always agree.
                nxt = datetime.fromtimestamp(next_pass_at(started, args.every))
                print(f"  next pass at {nxt:%Y-%m-%d %H:%M} "
                      f"(every {args.every:g} h; Ctrl+C to stop)")
        finally:
            session_log.finish()

    stopped = False
    try:
        if args.once:
            one_pass()
        else:
            run_forever(one_pass, args.every)
    except KeyboardInterrupt:
        print("\n  stopped")
        stopped = True
    if stalled[0] or stopped:
        # Abandoned boards, and the ones Ctrl+C left running, still own a
        # thread that a normal exit would wait on. The logs are closed and
        # what a board has not committed rolls back (SQLite), so leave.
        sys.stdout.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        # Same guard webapp.py carries, for the same reason: this runs as a
        # console app, and a window opened from Explorer or the Startup
        # folder closes the instant the process dies. A failure BEFORE the
        # session log opens has nowhere else to go, so hold the window
        # open long enough to read it. Only when someone is watching --
        # under a scheduler or a pipe this must not block forever.
        print(f"\n  [!] harvest failed to start: {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            if sys.stdin and sys.stdin.isatty():
                input("  Press Enter to close...")
        except EOFError:
            pass
        sys.exit(1)
