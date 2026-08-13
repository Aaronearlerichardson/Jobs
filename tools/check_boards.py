#!/usr/bin/env python3
"""Board health canary — is each ATS platform still there and still shaped
the way our fetcher expects?

    python tools/check_boards.py                 # human-readable table
    python tools/check_boards.py --json out.json --markdown BOARDS.md

Coverage percentage cannot answer this question. The fetchers swallow HTTP
errors and return [] by design (one dead board must not abort a crawl), so a
platform that changes its JSON shape fails SILENTLY: job counts just quietly
drop. This runs the real fetcher against a known-good public board per ATS
and reports what actually came back.

Two things make the result trustworthy:

  * The keyword filter is widened to match everything. Fetchers apply
    `is_relevant()` internally, so "0 jobs" would otherwise conflate "board
    is broken" with "nothing matched your search terms". Here it measures
    the BOARD, not the profile.
  * Failures are classified rather than lumped together. A GitHub runner
    getting a 403 from an anti-bot service is not the same event as your
    parser breaking, and a canary that cries wolf gets ignored.

Statuses: ok | degraded (reachable, fewer postings than the floor) |
blocked (rate-limited/challenged — not our bug) | broken (4xx/5xx/exception)
"""

import argparse
import contextlib
import io
import json
import re
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # Windows consoles default to cp1252; the status glyphs are not in it.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import config                                     # noqa: E402
from scrapers.sources import ATS_REGISTRY         # noqa: E402

SAMPLES = Path(__file__).parent / "board_samples.toml"

# An anti-bot wall or a rate limit says nothing about our parser.
_BLOCKED_RE = re.compile(
    r"\b(401|403|429|451)\b|captcha|cloudflare|forbidden|rate.?limit|"
    r"too many requests|access denied", re.I)
_BROKEN_RE = re.compile(r"\b(404|4\d\d|5\d\d)\b|timeout|timed out|"
                        r"connection|ssl|json|decode", re.I)

STATUS_EMOJI = {"ok": "✅", "degraded": "⚠️", "blocked": "🚧", "broken": "❌"}


def widen_keyword_filter():
    """Make `is_relevant()` accept everything, in place.

    filters.py bound these list objects at import time, so they must be
    mutated rather than rebound (see runner.apply_keyword_focus)."""
    config.CORE_KEYWORDS[:] = [""]      # "" is a substring of any text
    config.DOMAIN_KEYWORDS[:] = []
    config.SKILL_KEYWORDS[:] = []
    config.INCLUDE_KEYWORDS[:] = [""]
    config.EXCLUDE_PHRASES[:] = []
    config.EXCLUDE_TITLE_PHRASES[:] = []
    config.ACCEPT_REMOTE = True


def check_board(entry):
    """Fetch one board and classify the outcome."""
    ats, name, slug = entry["ats"], entry["name"], entry["slug"]
    floor = int(entry.get("min_jobs", 1))
    reg = ATS_REGISTRY.get(ats)
    if not reg:
        return {"ats": ats, "name": name, "status": "broken", "jobs": 0,
                "detail": f"no fetcher registered for {ats!r}", "seconds": 0.0}

    buf, started = io.StringIO(), time.monotonic()
    try:
        # Fetchers print their diagnostics; capture them to classify.
        with contextlib.redirect_stdout(buf):
            jobs = reg[0](name, slug)()
        note = " ".join(buf.getvalue().split())
        n = len(jobs)
        if n >= floor:
            status, detail = "ok", ""
        elif _BLOCKED_RE.search(note):
            status, detail = "blocked", note[:160]
        elif note:
            status, detail = "broken", note[:160]
        else:
            # 200 + parsed + zero postings: either a genuinely empty board or
            # a silent shape change. Worth a look, not an alarm.
            status = "degraded"
            detail = f"reachable but returned {n} (floor {floor})"
    except Exception as e:
        note = f"{type(e).__name__}: {e}"
        status = "blocked" if _BLOCKED_RE.search(note) else "broken"
        detail, n = note[:160], 0

    return {"ats": ats, "name": name, "status": status, "jobs": n,
            "detail": detail, "seconds": round(time.monotonic() - started, 1)}


def render_markdown(results, checked_at):
    ok = sum(r["status"] == "ok" for r in results)
    lines = [
        "# Board health",
        "",
        f"_{ok}/{len(results)} platforms healthy · checked "
        f"{checked_at} · [how this works]"
        "(tools/check_boards.py)_",
        "",
        "One request per platform against a public sample board, with the "
        "keyword filter widened so the number reflects the BOARD rather than "
        "any particular search profile.",
        "",
        "| | Platform | Sample board | Postings | Detail |",
        "|---|---|---|---:|---|",
    ]
    for r in sorted(results, key=lambda r: (r["status"] != "ok", r["ats"])):
        lines.append(f"| {STATUS_EMOJI.get(r['status'], '?')} | `{r['ats']}` "
                     f"| {r['name']} | {r['jobs']} | {r['detail'] or r['status']} |")
    lines += [
        "",
        "**✅ ok** — endpoint alive, response parsed, postings returned.  ",
        "**⚠️ degraded** — reachable and parsed, but fewer postings than "
        "expected: an empty board, or a silent shape change worth checking.  ",
        "**🚧 blocked** — rate-limited or challenged (commonly a CI runner's "
        "IP). Says nothing about the parser.  ",
        "**❌ broken** — 4xx/5xx or an exception: a real failure.",
        "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="ATS board health canary")
    ap.add_argument("--json", type=Path, help="write machine-readable results")
    ap.add_argument("--markdown", type=Path, help="write a status table")
    ap.add_argument("--badge", type=Path,
                    help="write a shields.io endpoint JSON")
    ap.add_argument("--fail-on-broken", action="store_true",
                    help="exit 1 if any board is broken (blocked never fails)")
    args = ap.parse_args()

    entries = tomllib.loads(SAMPLES.read_text(encoding="utf-8"))["board"]
    widen_keyword_filter()

    results = []
    for entry in entries:
        r = check_board(entry)
        results.append(r)
        print(f"  {STATUS_EMOJI.get(r['status'], '?')} {r['ats']:12} "
              f"{r['name'][:24]:24} {r['jobs']:5} postings  {r['seconds']:5.1f}s"
              f"  {r['detail']}")
        time.sleep(1.0)                            # politeness between hosts

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ok = sum(r["status"] == "ok" for r in results)
    broken = [r for r in results if r["status"] == "broken"]
    print(f"\n  {ok}/{len(results)} healthy"
          + (f", {len(broken)} broken" if broken else ""))

    if args.json:
        args.json.write_text(json.dumps(
            {"checked_at": checked_at, "healthy": ok, "total": len(results),
             "boards": results}, indent=1), encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(render_markdown(results, checked_at),
                                 encoding="utf-8")
    if args.badge:
        colour = ("brightgreen" if ok == len(results)
                  else "yellow" if ok >= len(results) * 0.7 else "red")
        args.badge.parent.mkdir(parents=True, exist_ok=True)
        args.badge.write_text(json.dumps(
            {"schemaVersion": 1, "label": "boards",
             "message": f"{ok}/{len(results)} healthy", "color": colour}),
            encoding="utf-8")
    return 1 if (args.fail_on_broken and broken) else 0


if __name__ == "__main__":
    sys.exit(main())
