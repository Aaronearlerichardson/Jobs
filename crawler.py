#!/usr/bin/env python3
"""
Job Crawler - CLI entry point.

One project, two pivots of the same search (see jobcrawler/tracks/):

    python crawler.py --track remote-neural   # REMOTE roles, neural-anchored
    python crawler.py --track local-tech      # LOCAL (Triangle/NC) roles,
                                              # health/bio/science mission
    python crawler.py                         # classic keyword crawl + email

Track flags pass through, e.g.:
"""

import argparse

import config
from jobcrawler.claude import expand_location, expand_search, score_technical_bar
from jobcrawler.report import (
    generate_keyword_report,
    print_expansion,
    print_location_expansion,
    send_email,
    write_report,
)


def main():
    ap = argparse.ArgumentParser(description="Job Crawler")
    ap.add_argument("--track", choices=("remote-neural", "local-tech"),
                    help="Run one of the job-search tracks (see jobcrawler/tracks/). "
                         "Remaining flags are forwarded to the track runner.")
    # Legacy aliases for --track local-tech.
    ap.add_argument("--local-clinical", "--local-tech", dest="local_tech",
                    action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--export-companies", metavar="PATH",
                    help="Dump the company roster to JSON (share/backup)")
    ap.add_argument("--import-companies", metavar="PATH",
                    help="Upsert companies from an exported JSON roster")
    ap.add_argument("--import-seeds", action="store_true",
                    help=argparse.SUPPRESS)   # retired: roster lives in the DB
    ap.add_argument("--dry-run", action="store_true",
                    help="Classic crawl: scan without DB writes or email")
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
    ap.add_argument("--score", metavar="TEXT",
                    help="Score one job title/description on technical bar (0..1) and exit")
    ap.add_argument("--nlx", metavar="COMPANIES",
                    help="Pull NC postings for comma-separated employers from the "
                         "NLx public feed (CareerOneStop API; covers bot-gated "
                         "federal contractors like Meta/Google/Qualcomm) and "
                         "ingest through the local-tech pipeline")
    ap.add_argument("--db", metavar="PATH",
                    help="Override the unified store DB path (isolates concurrent runs)")
    args, passthrough = ap.parse_known_args()

    if args.db:
        from pathlib import Path
        config.STORE_DB_PATH = Path(args.db)
        config.DB_PATH = config.STORE_DB_PATH  # legacy readers

    if args.import_seeds:
        print("  --import-seeds is retired: the company roster lives in the DB.\n"
              "  Manage it with discover.py (--local / --add-board / --apply) or\n"
              "  crawler.py --import-companies roster.json / --export-companies roster.json")
        raise SystemExit(0)

    if args.export_companies or args.import_companies:
        from jobcrawler import store
        conn = store.connect()
        if args.export_companies:
            n = store.export_companies(conn, args.export_companies)
            print(f"  exported {n} compan(ies) -> {args.export_companies}")
        if args.import_companies:
            n = store.import_companies(conn, args.import_companies)
            print(f"  imported/refreshed {n} compan(ies) from {args.import_companies}")
        conn.close()
        raise SystemExit(0)

    if args.score:
        score, reason, mission = score_technical_bar(args.score)
        if score is None:
            print("  [!] Scorer unavailable (set ANTHROPIC_API_KEY).")
        else:
            print(f"  technical-bar score: {score:.2f}  [{mission or 'mission?'}]  ({reason})")
        raise SystemExit(0)

    if args.nlx:
        from jobcrawler.fetchers.careeronestop import fetch_nlx_company
        from jobcrawler.tracks.local_tech import ingest_external_jobs
        total = 0
        for name in [n.strip() for n in args.nlx.split(",") if n.strip()]:
            jobs = fetch_nlx_company(name)
            print(f"  {name}: {len(jobs)} NLx posting(s) in NC")
            if jobs:
                total += ingest_external_jobs(jobs, source="nlx")
        print(f"\n  {total} new job(s) ingested from the NLx feed.")
        raise SystemExit(0)

    if args.local_tech and not args.track:
        args.track = "local-tech"

    if args.track == "remote-neural":
        from jobcrawler.tracks.remote_neural_run import main as run_track
        run_track(passthrough)
        raise SystemExit(0)

    if args.track == "local-tech":
        tp = argparse.ArgumentParser()
        tp.add_argument("--top", type=int, default=15)
        tp.add_argument("--workers", type=int, default=6)
        tp.add_argument("--rescore", action="store_true",
                        help="Re-score ALL stored jobs against the current "
                             "resume/prompt instead of crawling")
        targs = tp.parse_args(passthrough)
        if targs.rescore:
            from jobcrawler.tracks.local_tech import rescore_all
            rescore_all(max_workers=targs.workers)
        else:
            from jobcrawler.tracks.local_tech import run as run_track
            run_track(max_workers=targs.workers, top_n=targs.top)
        raise SystemExit(0)

    if passthrough:
        ap.error(f"unrecognized arguments: {' '.join(passthrough)} "
                 f"(track flags require --track)")

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

    from jobcrawler.orchestrator import crawl
    new_jobs    = crawl(dry_run=args.dry_run)
    report_path = write_report(new_jobs)
    if not args.dry_run:
        send_email(new_jobs, report_path)


if __name__ == "__main__":
    main()
