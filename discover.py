#!/usr/bin/env python3
"""
discover.py - expand the crawler's company universe.

Asks Claude for likely employers in a sector, then probes public ATSes
(Greenhouse/Lever/Ashby/Kula) to confirm which slugs are real. Outputs
copy-paste dict entries you can drop straight into config.py.

Usage:
    python discover.py "neurotech startups"
    python discover.py "medical device companies hiring ML engineers"
"""

import argparse
import sys

from config import INCLUDE_KEYWORDS
from jobcrawler.discovery import (
    apply_to_config,
    bciwiki_seed_candidates,
    discover,
    discover_companies,
    print_summary,
    write_discovery_report,
)


def main():
    ap = argparse.ArgumentParser(description="Expand the crawler's company universe.")
    ap.add_argument("term", nargs="?",
                    help="Sector/industry/term to search for (e.g. 'neurotech startups')")
    ap.add_argument("--from-keywords", action="store_true",
                    help="Run discovery for each entry in INCLUDE_KEYWORDS")
    ap.add_argument("--from-bciwiki", action="store_true",
                    help="Resolve the BCIWiki company directory "
                         "(bciwiki.org Category:Companies) to crawlable boards")
    ap.add_argument("--bciwiki-categories", default="companies",
                    help="Comma-separated BCIWiki categories to harvest "
                         "(companies,labs,organizations). Default: companies")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of candidates resolved (for testing)")
    ap.add_argument("--js", action="store_true",
                    help="Enable the headless-browser Workday fallback for "
                         "--from-bciwiki (off by default for bulk: it's "
                         "single-threaded and dominates a large run)")
    ap.add_argument("--local", action="store_true",
                    help="NC local-sourcing pass: curated seeds + RTP directory "
                         "+ careers-page sniffing -> NC-verified boards, "
                         "mission-scored into the company store")
    ap.add_argument("--dork", action="store_true",
                    help="ATS dorking via DuckDuckGo: mine search-indexed ATS "
                         "board URLs for NC companies into the company store")
    ap.add_argument("--no-report", action="store_true",
                    help="Print to stdout only, don't write a markdown report")
    ap.add_argument("--apply", action="store_true",
                    help="Insert confirmed candidates into config.py in place "
                         "(deduped by slug, tagged with date/term for audit)")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --apply, preview changes without writing the file")

    args = ap.parse_args()

    if args.from_keywords:
        for kw in INCLUDE_KEYWORDS:
            result = discover(kw)
            print_summary(result)
            if not args.no_report:
                write_discovery_report(result)
            if args.apply:
                for line in apply_to_config(result, dry_run=args.dry_run):
                    print(line)
        return

    if args.from_bciwiki:
        cats = tuple(c.strip() for c in args.bciwiki_categories.split(",") if c.strip())
        print(f"  > Harvesting BCIWiki categories: {', '.join(cats)}")
        seeds = bciwiki_seed_candidates(categories=cats)
        if args.limit:
            seeds = seeds[: args.limit]
        print(f"  > {len(seeds)} candidate(s) to resolve")
        result = discover_companies(seeds, term=f"bciwiki:{','.join(cats)}",
                                    use_js=args.js)
        print_summary(result)
        if not args.no_report:
            write_discovery_report(result)
        if args.apply:
            for line in apply_to_config(result, dry_run=args.dry_run):
                print(line)
        return

    if args.local:
        from jobcrawler.discovery.local_sourcing import populate_companies
        populate_companies()
        return

    if args.dork:
        from jobcrawler.discovery.ats_dork import run_ddgs_dorks
        added, checked = run_ddgs_dorks()
        print(f"\n  {added} new NC board(s) added to the store "
              f"({checked} extracted from dork results)")
        return

    if not args.term:
        ap.print_help()
        sys.exit(1)

    result = discover(args.term)
    print_summary(result)
    if not args.no_report:
        write_discovery_report(result)
    if args.apply:
        for line in apply_to_config(result, dry_run=args.dry_run):
            print(line)


if __name__ == "__main__":
    main()
