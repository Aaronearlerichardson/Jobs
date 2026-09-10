#!/usr/bin/env python3
"""Background whole-board harvester (see scrapers/harvest.py).

    python harvest.py                       # a pass now, then one every 12 h
    python harvest.py --every 8             # ... every 8 h instead
    python harvest.py --once                # one pass, then exit
    python harvest.py --list                # print the plan, fetch nothing
    python harvest.py --only greenhouse,lever --limit 5 --once
    python harvest.py --names "NVIDIA" "IQVIA" --once   # named boards, even if fresh
    python harvest.py --max-hours 6         # abandon a pass still running after 6 h

Meant to sit in the background for the whole session: put a shortcut to
JobHarvester.exe in the Startup folder (Win+R, `shell:startup`) and it
starts at log-on, runs a pass, and parks on a timed wait until the next
one. Parked, it costs no CPU at all -- the thread is not scheduled until
its deadline -- and the deadline is wall-clock, so a laptop that slept
through it runs the pass as soon as it wakes. Each pass pulls every board
with a fetchable ATS that has not been harvested in the last
--min-age-hours and stores every posting unscored; nothing here calls
Claude or reads the resume. A second copy started while one is running
exits at once (lock file in the data directory). Each pass gets its own
session log (data/logs/session-*-harvest.log).
"""

import argparse
import os
import sys
import threading
import time
import traceback
from datetime import datetime

import config

try:  # Windows consoles default to cp1252; job text carries em-dashes etc.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOCK_PATH = config.DATA_DIR / "harvest.lock"
DEFAULT_EVERY_HOURS = 12.0
# Longest single wait between deadline checks. Short enough that a machine
# waking from a long sleep notices an overdue pass within minutes; a timed
# wait costs nothing while it lasts, so the chunking is free.
WAIT_CHUNK_S = 300.0


def acquire_lock(path=LOCK_PATH):
    """Hold an OS-level exclusive lock on `path` for the life of the
    process (released by the OS on any exit, so a crash never leaves a
    stale lock). Returns the open handle, or None when another harvester
    already holds it."""
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
    """Run `pass_fn` now and then once every `every_hours`, measured from
    the START of the previous pass (a pass that takes three hours does not
    push the schedule back). Never returns unless `wait` asks it to.

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
    >>> def one_pass():
    ...     log.append(clock())
    ...     ticks[0] += 3600          # the pass itself takes an hour
    ...     if len(log) == 3:
    ...         raise KeyboardInterrupt
    >>> try:
    ...     run_forever(one_pass, 12, wait=wait, clock=clock)
    ... except KeyboardInterrupt:
    ...     pass
    >>> [t / 3600 for t in log]
    [0.0, 12.0, 24.0]
    """
    wait = wait or threading.Event().wait
    while True:
        started = clock()
        pass_fn()
        while True:
            remaining = started + every_hours * 3600 - clock()
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
                         "(default 6)")
    ap.add_argument("--max-hours", type=float,
                    help="Abandon whatever is still running after this long")
    ap.add_argument("--workers", type=int, default=None,
                    help="Boards in flight at once (default: n_cpus-1, "
                         "or HARVEST_WORKERS)")
    ap.add_argument("--no-hydrate", action="store_true",
                    help="Store listings only; skip the per-posting detail "
                         "fetches")
    ap.add_argument("--db", help="Store path (default: the data dir's jobs.db)")
    args = ap.parse_args(argv)

    from core import session_log
    from scrapers import harvest

    only = ({s.strip() for s in args.only.split(",") if s.strip()}
            if args.only else None)
    min_age = (harvest.MIN_AGE_HOURS if args.min_age_hours is None
               else args.min_age_hours)

    if args.list:
        from core import store
        conn = store.connect(args.db)
        boards = harvest.plan(conn, only=only, names=args.names,
                              min_age_hours=min_age, limit=args.limit)
        conn.close()
        for c in boards:
            print(f"  {c['name']}  ({c['ats']}, ~{c.get('total_job_count') or 0}"
                  f" jobs, last harvested {c.get('last_harvested_at') or 'never'})")
        print(f"  {len(boards)} board(s)")
        return 0

    lock = acquire_lock()
    if lock is None:
        print("  [!] another harvester holds the lock; exiting")
        return 0

    stalled = [0]

    def one_pass():
        # A fresh session log per pass, so each shows up in data/logs on
        # its own and retention pruning treats it like any other run.
        session_log.start(["--harvest", *argv])
        try:
            try:
                summary = harvest.run(
                    db_path=args.db, only=only, names=args.names,
                    min_age_hours=min_age, limit=args.limit,
                    max_workers=args.workers or harvest.DEFAULT_WORKERS,
                    hydrate=not args.no_hydrate, max_hours=args.max_hours)
                stalled[0] += summary["stalled"]
            except Exception:
                # Put the traceback in the session log while it is still
                # open: with no console it is the only place output goes.
                # A pass that dies must not take the timer down with it;
                # --once is the interactive case and should fail loudly.
                print("\n  [!] harvest pass failed:", file=sys.stderr)
                traceback.print_exc()
                if args.once:
                    raise
            if not args.once:
                nxt = datetime.fromtimestamp(time.time() + args.every * 3600)
                print(f"  next pass at {nxt:%Y-%m-%d %H:%M} "
                      f"(every {args.every:g} h; Ctrl+C to stop)")
        finally:
            session_log.finish()

    try:
        if args.once:
            one_pass()
        else:
            run_forever(one_pass, args.every)
    except KeyboardInterrupt:
        print("\n  stopped")
    if stalled[0]:
        # Abandoned boards still own a thread; a normal exit would wait on
        # them. Everything is committed and the logs are closed, so leave.
        sys.stdout.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
