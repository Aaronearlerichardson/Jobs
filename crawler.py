#!/usr/bin/env python3
"""
BCI Job Crawler - CLI entry point.

Usage:
    python crawler.py                         # full run
    python crawler.py --dry-run               # print only, no DB/email
    python crawler.py --expand "eeg engineer" # print expanded titles/keywords/sectors
    python crawler.py --expand-location "NC"  # expand a location term
    python crawler.py --keyword-report        # bulk-expand every INCLUDE_KEYWORDS entry
    python crawler.py --local-clinical        # LOCAL-CLINICAL-ML track (live, ranked, no email)
    python crawler.py --score "..."           # score one description on technical bar (0..1)
    python crawler.py --db jobs_local.db ...   # use an isolated dedupe DB

Edit config.py to tune keywords, locations, and target companies.
"""

import argparse

import config
from jobcrawler.claude import expand_location, expand_search, score_technical_bar
from jobcrawler.orchestrator import crawl
from jobcrawler.report import (
    generate_keyword_report,
    print_expansion,
    print_location_expansion,
    send_email,
    write_report,
)


def main():
    ap = argparse.ArgumentParser(description="BCI Job Crawler")
    ap.add_argument("--dry-run", action="store_true",
                    help="Scan without DB writes or email")
    ap.add_argument("--expand", metavar="TERM",
                    help="Expand a term into job titles/keywords/sectors and exit")
    ap.add_argument("--expand-live", metavar="TERM",
                    help="Expand a term and fold results into this crawl run")
    ap.add_argument("--expand-location", metavar="TERM",
                    help="Expand a location term into include/exclude substrings and exit")
    ap.add_argument("--expand-location-live", metavar="TERM",
                    help="Expand a location term and fold results into this crawl run")
    ap.add_argument("--keyword-report", action="store_true",
                    help="Bulk-expand every INCLUDE_KEYWORDS entry and write a suggestions report")
    ap.add_argument("--local-clinical", action="store_true",
                    help="Run the LOCAL-CLINICAL-ML track: live crawl, Triangle/NC "
                         "+ remote geo filter, clinical/health domain targeting, "
                         "ops/defense excludes, technical-bar scoring, ranked digest. "
                         "Writes dedup state but never emails.")
    ap.add_argument("--score", metavar="TEXT",
                    help="Score one job title/description on technical bar (0..1) and exit")
    ap.add_argument("--db", metavar="PATH",
                    help="Override the dedupe DB path (isolates concurrent runs)")
    args = ap.parse_args()

    if args.db:
        from pathlib import Path
        config.DB_PATH = Path(args.db)

    if args.score:
        score, reason, mission = score_technical_bar(args.score)
        if score is None:
            print("  [!] Scorer unavailable (set ANTHROPIC_API_KEY).")
        else:
            print(f"  technical-bar score: {score:.2f}  [{mission or 'mission?'}]  ({reason})")
        raise SystemExit(0)

    if args.local_clinical:
        from jobcrawler.local_clinical import run as run_local_clinical
        run_local_clinical(db_path=config.DB_PATH if args.db else None)
        raise SystemExit(0)

    if args.expand:
        expanded = expand_search(args.expand)
        if expanded:
            print_expansion(args.expand, expanded)
        raise SystemExit(0)

    if args.expand_location:
        expanded = expand_location(args.expand_location)
        if expanded:
            print_location_expansion(args.expand_location, expanded)
        raise SystemExit(0)

    if args.keyword_report:
        generate_keyword_report()
        raise SystemExit(0)

    if args.expand_live:
        print(f"\n  Expanding search for '{args.expand_live}'...")
        expanded = expand_search(args.expand_live)
        if expanded:
            print_expansion(args.expand_live, expanded)
            added = []
            for term in expanded.get("titles", []) + expanded.get("keywords", []):
                kw = term.lower()
                if kw not in config.INCLUDE_KEYWORDS:
                    config.INCLUDE_KEYWORDS.append(kw)
                    added.append(kw)
            if added:
                print(f"  + {len(added)} new keyword(s) added to this run.\n")

    if args.expand_location_live:
        print(f"\n  Expanding location '{args.expand_location_live}'...")
        expanded = expand_location(args.expand_location_live)
        if expanded:
            print_location_expansion(args.expand_location_live, expanded)
            added_inc, added_exc = [], []
            for loc in expanded.get("include", []):
                if loc.lower() not in [i.lower() for i in config.LOCATION_INCLUDE]:
                    config.LOCATION_INCLUDE.append(loc.lower())
                    added_inc.append(loc)
            for loc in expanded.get("exclude", []):
                if loc.lower() not in [i.lower() for i in config.LOCATION_EXCLUDE]:
                    config.LOCATION_EXCLUDE.append(loc.lower())
                    added_exc.append(loc)
            if added_inc or added_exc:
                print(f"  + {len(added_inc)} include / {len(added_exc)} "
                      f"exclude location filter(s).\n")

    new_jobs    = crawl(dry_run=args.dry_run)
    report_path = write_report(new_jobs)
    if not args.dry_run:
        send_email(new_jobs, report_path)


if __name__ == "__main__":
    main()
