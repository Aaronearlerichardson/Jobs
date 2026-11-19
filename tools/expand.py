#!/usr/bin/env python3
"""Keyword/location expansion utilities — an LLM suggests, you decide.

    python tools/expand.py "eeg engineer"        # alt titles/keywords/sectors
    python tools/expand.py --location "NC"       # location synonym expansion
    python tools/expand.py --keyword-report      # bulk-expand profile keywords

The suggestion side of the old report module: expand a seed term into
titles/keywords/sectors, expand a location, or bulk-expand every configured
keyword into a suggestions report. Report-only and LLM-billed — it informs
a crawl rather than being part of one — so it lives under tools/ beside the
other analysis CLIs instead of on the crawl entry point. Results are
SUGGESTIONS to copy into profile.toml (or the Settings tab); nothing here
mutates config or the store.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Collection
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, runstate  # noqa: E402

from src.claude.api import ExpandReply, LocationReply, expand_location, expand_search  # noqa: E402

BAR = "=" * 62
INCLUDE_KEYWORDS = config.INCLUDE_KEYWORDS
LOCATION_INCLUDE = config.LOCATION_INCLUDE
LOCATION_EXCLUDE = config.LOCATION_EXCLUDE
REPORT_DIR = config.REPORT_DIR


# ─── Expansion pretty-printers ────────────────────────────────────────────

def _banner(title: str) -> None:
    print(f"\n{BAR}\n  {title}\n{BAR}")


def _section(title: str, items: list[str], known: Collection[str] = ()) -> None:
    """`items` under `title`, each marked when `known` already holds it."""
    seen = {k.lower() for k in known}
    print(f"\n  {title} ({len(items)})")
    for x in items:
        print(f"    - {x}{'  [already in list]' if x.lower() in seen else ''}")


def _footer(*lines: str) -> None:
    print(f"\n  {'-'*58}")
    for line in lines:
        print(f"  {line}")
    print(f"{BAR}\n")


def print_expansion(term: str, expanded: ExpandReply) -> None:
    track = config.UI_TRACKS.get(config.DEFAULT_TRACK or "")
    _banner(f"{track.label if track else 'Search'} Expansion: '{term}'")
    _section("JOB TITLES TO SEARCH", expanded.titles)
    _section("KEYWORDS TO ADD", expanded.keywords, INCLUDE_KEYWORDS)
    _section("SECTORS / COMPANIES TO INVESTIGATE", expanded.sectors)
    _footer("To fold these into a live crawl, rerun with:",
            "  add the keywords to profile.toml [keywords] (Settings tab)")


def print_location_expansion(term: str, expanded: LocationReply) -> None:
    _banner(f"Location Expansion: '{term}'")
    _section("LOCATION_INCLUDE additions", expanded.include, LOCATION_INCLUDE)
    _section("LOCATION_EXCLUDE additions", expanded.exclude, LOCATION_EXCLUDE)
    _footer("Copy entries you want into LOCATION_INCLUDE / LOCATION_EXCLUDE.")


# ─── Bulk keyword report ──────────────────────────────────────────────────

async def generate_keyword_report(delay: float = 0.5) -> Path:
    """
    Expand every INCLUDE_KEYWORDS entry via Claude, aggregate unique
    new titles/keywords/sectors, write a markdown report.
    """
    REPORT_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = REPORT_DIR / f"keyword_expansion_{date_str}.md"

    existing_kw = {k.lower() for k in INCLUDE_KEYWORDS}
    all_titles: dict[str, list[str]] = {}
    all_keywords: dict[str, list[str]] = {}
    all_sectors: dict[str, list[str]] = {}

    bar = "=" * 62
    print(f"\n{bar}")
    print(f"  Keyword Report - expanding {len(INCLUDE_KEYWORDS)} keyword(s)")
    print(f"{bar}\n")

    for i, kw in enumerate(INCLUDE_KEYWORDS, 1):
        print(f"  [{i}/{len(INCLUDE_KEYWORDS)}] '{kw}'")
        expanded = await expand_search(kw)
        if not expanded:
            continue
        for t in expanded.titles:
            all_titles.setdefault(t, []).append(kw)
        for k in expanded.keywords:
            all_keywords.setdefault(k, []).append(kw)
        for s in expanded.sectors:
            all_sectors.setdefault(s, []).append(kw)
        await asyncio.sleep(delay)

    def sort_by_freq(d: dict[str, list[str]]) -> list[tuple[str, list[str]]]:
        return sorted(d.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Keyword Expansion Report - {date_str}\n\n")
        f.write(f"Seeded from **{len(INCLUDE_KEYWORDS)}** existing keyword(s) in `INCLUDE_KEYWORDS`.\n")
        f.write("Suggestions ranked by how many seed terms surfaced them.\n\n")

        f.write("## New keywords to consider\n\n")
        f.write("Items marked `[already in list]` are in `INCLUDE_KEYWORDS`.\n\n")
        f.write("| Suggestion | Surfaced by | Status |\n|---|---|---|\n")
        for term, seeds in sort_by_freq(all_keywords):
            flag = "already in list" if term.lower() in existing_kw else "NEW"
            f.write(f"| `{term}` | {len(seeds)} | {flag} |\n")

        f.write("\n## Alternative job titles to search\n\n")
        f.write("| Title | Surfaced by |\n|---|---|\n")
        for term, seeds in sort_by_freq(all_titles):
            f.write(f"| {term} | {len(seeds)} |\n")

        f.write("\n## Sectors / employers to investigate\n\n")
        f.write("Pass any of these to `discover.py` to get ATS slug candidates.\n\n")
        f.write("| Sector / Employer | Surfaced by |\n|---|---|\n")
        for term, seeds in sort_by_freq(all_sectors):
            f.write(f"| {term} | {len(seeds)} |\n")

        new_only = [t for t in all_keywords if t.lower() not in existing_kw]
        if new_only:
            f.write("\n## Copy-paste block (new keywords only)\n\n")
            f.write("```python\n")
            for t in sorted(new_only, key=str.lower):
                f.write(f'    "{t.lower()}",\n')
            f.write("```\n")

    print(f"\n  Report -> {path}\n")
    return path


# ─── CLI ────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="Expand a term, a location, or your whole keyword list "
                    "into suggestions. Reports only — nothing is written to "
                    "profile.toml or the store.")
    ap.add_argument("term", nargs="?",
                    help="Seed term to expand into titles/keywords/sectors")
    ap.add_argument("--location", metavar="TERM",
                    help="Expand a location into include/exclude synonyms")
    ap.add_argument("--keyword-report", action="store_true",
                    help="Bulk-expand every configured keyword into a report")
    args = ap.parse_args(argv)

    if args.location:
        located = runstate.run(expand_location(args.location))
        if located:
            print_location_expansion(args.location, located)
        return
    if args.keyword_report:
        runstate.run(generate_keyword_report())
        return
    if not args.term:
        ap.print_help()
        sys.exit(1)
    expanded = runstate.run(expand_search(args.term))
    if expanded:
        print_expansion(args.term, expanded)


if __name__ == "__main__":
    main()
