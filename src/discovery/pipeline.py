"""Sector discovery: Claude -> employer names -> the shared resolver -> report.

The SOURCING half of a `discover-term` run: ask Claude for employer names
matching a term, merge the profile's seeds (src.discovery.seeds), and hand
them to local_sourcing.queue_names, the resolve-and-queue path every name
list takes. What comes back is printed and written as a markdown report.

Notes:
    This module used to carry its own candidate type, resolver wrapper
    (validate_candidate) and store write (apply.apply_to_store), with the
    detection-only lead and headless-scan fallbacks only it had. The lead
    is classify_miss's ``ats-unsupported:<ats>`` reason and the scan is
    resolve_or_miss's `js` now, so one path resolves and queues every name.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TypedDict

from src import store
from src.claude.api import DISCOVER_SYSTEM, DiscoverReply, call_claude_json
from src.config import REPORT_DIR, SETTINGS
from src.match.names import strip_suffixes
from src.net.util import worker_count
from src.rows import BoardHit
from .local_sourcing import queue_names
from .seeds import seed_names_for

# Parallel worker count for the resolution: a network-concurrency knob,
# not a CPU one (DISCOVERY_WORKERS). Tune down if you see 429s.
_DISCOVERY_WORKERS = worker_count("discovery_workers")


class DiscoveryResult(TypedDict):
    """What `discover` returns."""
    term: str
    queued: list[BoardHit]
    missed: list[tuple[str, str]]           # (name, miss reason)
    gated_sites: list[dict[str, str]]       # `GatedSite` dumps


#: Why a resolved board still deserves a human glance, keyed by HOW it was
#: found. A sniff read the coordinates off the company's OWN careers page,
#: the one provenance that cannot collide with a same-named stranger.
_VIA_NOTES = {
    "probe":     "name-guessed slug, not read off the company's own site "
                 "- confirm identity",
    "websearch": "found by web search, not on the company's own site",
    "js":        "headless careers-page scan - confirm identity",
    "directory": "matched by name in the public board directory "
                 "- confirm identity",
}


def verify_note(hit: BoardHit) -> str:
    """Why `hit` needs a human look, or ''.

    >>> verify_note({"via": "probe"}).startswith("name-guessed")
    True
    >>> verify_note({"via": "sniff"})
    ''
    """
    return _VIA_NOTES.get(hit.get("via") or "", "")


def _merge_seeds(names: dict[str, str], seeds: list[str]) -> dict[str, str]:
    """`names` (name -> careers URL), then each seed whose normalized name
    it lacks.

    >>> _merge_seeds({"Acme Inc.": "https://acme.example"}, ["Acme", "Beta"])
    {'Acme Inc.': 'https://acme.example', 'Beta': ''}
    """
    seen = {strip_suffixes(n).lower() for n in names}
    return names | {s: "" for s in seeds if strip_suffixes(s).lower() not in seen}


async def discover(term: str, dry_run: bool = False) -> DiscoveryResult:
    """Ask Claude for employers matching `term`, merge the seeds, and
    resolve and queue each for review (queue_names: every board, local or
    not, tagged as its platform seeds, with the headless scan for SPA
    careers pages). `dry_run` resolves without writing."""
    print(f"  > Asking Claude for companies in: {term!r}")
    payload = await call_claude_json(DISCOVER_SYSTEM, term, max_tokens=2000,
                                     reply=DiscoverReply)
    suggested = {c.name.strip(): c.careers_url for c in payload.companies
                 if c.name.strip()} if payload else {}
    names = _merge_seeds(suggested, seed_names_for(term))
    gated = [g.model_dump() for g in payload.gated_sites] if payload else []
    print(f"  > Claude returned {len(suggested)} suggestion(s)"
          + (f" + {len(names) - len(suggested)} seed(s) merged"
             if len(names) > len(suggested) else ""))
    async with store.Writer() as db:
        queued, missed = await queue_names(
            db, dict.fromkeys(names, f"discovery:{term[:60]}"),
            {n: u for n, u in names.items() if u}, _DISCOVERY_WORKERS,
            local_only=False, seed_tags=True,
            js_pages=min(SETTINGS.js_pages, _DISCOVERY_WORKERS), dry_run=dry_run)
    return {"term": term, "queued": queued, "missed": missed, "gated_sites": gated}


# ─── Report ──────────────────────────────────────────────────────────────

def _handle(hit: BoardHit) -> str:
    """The board's coordinate as one string: its handle, else its URL.

    >>> _handle({"ats": "workday", "slug": ("ds", 5, "Ext")})
    'ds/5/Ext'
    >>> _handle({"ats": "custom", "slug": None, "careers_url": "https://x.example/"})
    'https://x.example/'
    """
    slug = hit.get("slug")
    if isinstance(slug, tuple):
        return "/".join(map(str, slug))
    return str(slug or hit.get("careers_url") or "-")


def write_discovery_report(result: DiscoveryResult) -> Path:
    """Write `result` as REPORT_DIR/discover_<date>_<term>.md; return the path."""
    REPORT_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    slug = "".join(c if c.isalnum() else "_" for c in result["term"].lower())[:40]
    path = REPORT_DIR / f"discover_{date_str}_{slug}.md"
    queued, missed = result["queued"], result["missed"]
    lines = [f"# Company Discovery - {result['term']}\n", f"_Generated {date_str}_\n",
             f"**{len(queued)}** resolved / {len(queued) + len(missed)} suggested\n"]
    if queued:
        lines += ["## Resolved - queued for review\n",
                  "| Company | ATS | Slug / coordinates | Jobs live | Local | Verify |",
                  "|---|---|---|---:|---:|---|"]
        lines += [f"| {h['name']} | {h['ats']} | `{_handle(h)}` | {h.get('count') or 0} "
                  f"| {h.get('nc') or 0} | {verify_note(h)} |" for h in queued]
        lines.append("")
    if missed:
        # ats-unsupported:<ats> is a platform recognized but not fetchable:
        # add the board by hand.
        lines += ["## Unresolved - manual investigation needed\n",
                  "| Company | Reason |", "|---|---|"]
        lines += [f"| {n} | {r} |" for n, r in missed]
        lines.append("")
    if result["gated_sites"]:
        lines += ["## Gated sites (require auth)\n",
                  "Login-only boards Claude thinks are worth searching. Browse them "
                  "logged-in and capture result pages with `python capture.py` "
                  "(see README.md).\n",
                  "| Site | Suggested query | Notes |", "|---|---|---|"]
        lines += [f"| {g.get('site', '?')} | `{g.get('query', '')}` | {g.get('notes', '')} |"
                  for g in result["gated_sites"]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n  Report -> {path}\n")
    return path


def print_summary(result: DiscoveryResult) -> None:
    """Print `result`: each resolved board (with its verify note) and each miss."""
    queued, missed = result["queued"], result["missed"]
    w = 62
    print(f"\n{'=' * w}\n  Discovery: '{result['term']}'\n{'=' * w}")
    print(f"  Resolved: {len(queued)} / Suggested: {len(queued) + len(missed)}\n")
    for h in queued:
        note = verify_note(h)
        print(f"    + {h['name']:<30} {h['ats']:<10} slug='{_handle(h)}'  "
              f"({h.get('count') or 0} jobs){f'  [VERIFY: {note}]' if note else ''}")
    if missed:
        print(f"\n  Unresolved ({len(missed)}):")
        for n, r in missed:
            print(f"    ? {n:<30} {r}")
    print(f"{'=' * w}\n")
