#!/usr/bin/env python3
"""
discover.py - build the roster of companies to crawl.

The crawler only searches employers it knows about, and that roster lives in
the store's `companies` table. This is how it grows: describe a sector and
Claude suggests employers, then every name is probed against the public
applicant-tracking systems (Greenhouse/Lever/Ashby/...) to confirm which
actually have a crawlable job board.

Usage:
    python discover.py "climate tech startups"
    python discover.py "medical device companies hiring ML engineers"
    python discover.py --local          # employers in your [locality]

Every flag except --from-bciwiki is the CLI spelling of an operation in
src/ops/registry.py, the same table the web UI's roster buttons run from.
"""

import argparse
import sys

from src import config
from src.ops import registry


def _op(name, params):
    """A handler that runs registry op `name` with `params(args)`."""
    def run(args):
        registry.invoke(name, params(args), track=None)
    return run


def _cmd_from_keywords(args):
    for kw in config.INCLUDE_KEYWORDS:
        registry.invoke("discover-term", {
            "term": kw, "no_report": args.no_report, "dry_run": args.dry_run},
            track=None)


def _cmd_from_bciwiki(args):
    """A worked example of bulk-importing a public industry directory: the
    BCIWiki company list, resolved to crawlable boards. Not a registry op —
    it is directory-specific and only useful if that is your field."""
    from src.discovery import (apply_to_store, bciwiki_seed_candidates,
                           discover_companies, print_summary,
                           write_discovery_report)
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
    for line in apply_to_store(result, dry_run=args.dry_run):
        print(line)


# In precedence order: the first whose flag is set runs and the process
# exits. `dest` is the argparse attribute that selects it.
_COMMANDS = [
    ("from_keywords", _cmd_from_keywords),
    ("from_bciwiki", _cmd_from_bciwiki),
    ("local", _op("discover-local", lambda a: {})),
    ("add_board", _op("add-board", lambda a: {
        "name": a.add_board[0], "url": a.add_board[1], "capture": a.capture})),
    ("score_missions", _op("score-missions", lambda a: {"rescore": a.rescore_missions})),
    ("rescore_missions", _op("score-missions", lambda a: {"rescore": a.rescore_missions})),
    ("resolve_leads", _op("resolve-leads", lambda a: {
        "all_leads": a.all_leads, "limit": a.limit})),
    ("dork", _op("dork", lambda a: {})),
]


def main():
    ap = argparse.ArgumentParser(description="Expand the crawler's company universe.")
    ap.add_argument("term", nargs="?",
                    help="Sector/industry/term to search for "
                         "(e.g. 'climate tech startups')")
    ap.add_argument("--from-keywords", action="store_true",
                    help="Run discovery once per keyword in your profile")
    ap.add_argument("--from-bciwiki", action="store_true",
                    help="Resolve the BCIWiki company directory "
                         "(bciwiki.org, ~700 brain-computer-interface "
                         "companies) to crawlable boards. A worked example of "
                         "bulk-importing a public industry directory; only "
                         "useful if that is your field.")
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
                    help="Local-sourcing pass for your [locality]: profile "
                         "seeds + configured directories + web-search name "
                         "harvesting -> probed, locality-verified, "
                         "mission-scored into the company store")
    ap.add_argument("--add-board", nargs=2, metavar=("NAME", "URL"),
                    help="Register a known board directly: company name + its ATS "
                         "board URL (or careers page). No guessing; "
                         "locality-verifies, mission-scores, activates.")
    ap.add_argument("--capture", action="store_true",
                    help="With --add-board: register the URL as a capture-only "
                         "company (ats 'capture'). Nothing is sniffed or fetched; "
                         "you browse the board yourself and save pages with "
                         "capture.py --watch, which files them under this row.")
    ap.add_argument("--score-missions", action="store_true",
                    help="Backfill mission scores for active companies that "
                         "have a board but no mission tier (seeds import "
                         "deliberately skips scoring)")
    ap.add_argument("--rescore-missions", action="store_true",
                    help="Re-score mission for ALL active companies")
    ap.add_argument("--resolve-leads", action="store_true",
                    help="Resolve boardless company leads (from capture.py) into "
                         "crawlable boards and activate the hits: careers-page "
                         "sniff first (collision-safe), slug-probe fallback, every "
                         "board validated by a live fetch")
    ap.add_argument("--all-leads", action="store_true",
                    help="With --resolve-leads: resolve EVERY inactive boardless "
                         "lead, not just capture.py's page_capture ones")
    ap.add_argument("--dork", "--ats-dork", action="store_true", dest="dork",
                    help="ATS dorking via DuckDuckGo: mine search-indexed ATS "
                         "board URLs for local companies into the company store")
    ap.add_argument("--no-report", action="store_true",
                    help="Print to stdout only, don't write a markdown report")
    ap.add_argument("--apply", action="store_true",
                    help="Deprecated no-op: confirmed candidates are applied "
                         "to the company store by default now (deduped by "
                         "slug, tagged with date/term for audit). Kept only "
                         "so old scripts/muscle memory that pass --apply "
                         "keep working.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Probe candidates and print/report on them WITHOUT "
                         "writing anything to the company store (the old "
                         "default behavior — use this to preview before "
                         "committing).")

    args = ap.parse_args()

    from src.config import bootstrap
    bootstrap.ensure_profile()

    for dest, handler in _COMMANDS:
        if getattr(args, dest):
            handler(args)
            return

    if not args.term:
        ap.print_help()
        sys.exit(1)

    registry.invoke("discover-term", {
        "term": args.term, "no_report": args.no_report, "dry_run": args.dry_run},
        track=None)


if __name__ == "__main__":
    main()
