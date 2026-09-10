#!/usr/bin/env python3
"""Background whole-board harvester (see scrapers/harvest.py).

    python harvest.py                       # every board due, then exit
    python harvest.py --list                # print the plan, fetch nothing
    python harvest.py --only greenhouse,lever --limit 5
    python harvest.py --names "NVIDIA" "IQVIA"   # named boards, even if fresh
    python harvest.py --max-hours 6         # stop submitting after 6 hours

Meant to run from Task Scheduler at log-on and every few hours after
(tools/register_harvest_task.ps1): each run pulls every board with a
fetchable board that has not been harvested in the last --min-age-hours,
stores every posting unscored, and exits. A second copy started while one
is running exits at once (lock file in the data directory). Nothing here
calls Claude or reads the resume; the daily crawl scores what this stores.
"""

import argparse
import os
import sys

import config

try:  # Windows consoles default to cp1252; job text carries em-dashes etc.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOCK_PATH = config.DATA_DIR / "harvest.lock"


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


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        description="Pull every board whole and store it unscored")
    ap.add_argument("--list", action="store_true",
                    help="Print the boards this run would pull, then exit")
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

    session_log.start(["--harvest", *argv])
    try:
        summary = harvest.run(
            db_path=args.db, only=only, names=args.names,
            min_age_hours=min_age, limit=args.limit,
            max_workers=args.workers or harvest.DEFAULT_WORKERS,
            hydrate=not args.no_hydrate, max_hours=args.max_hours)
    finally:
        session_log.finish()
    if summary["stalled"]:
        # Abandoned boards still own a thread; a normal exit would wait on
        # them. Everything is committed and the log is closed, so leave.
        sys.stdout.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
