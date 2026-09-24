#!/usr/bin/env python3
"""Board health canary — is each ATS platform still there and still shaped
the way our fetcher expects?

    python tools/check_boards.py                 # human-readable table
    python tools/check_boards.py --json out.json --markdown BOARDS.md
    python tools/check_boards.py --resolved workday   # one spec, resolved
    python tools/check_boards.py --promote            # default candidates

Coverage percentage cannot answer this question. The fetchers swallow HTTP
errors and return [] by design (one dead board must not abort a crawl), so a
platform that changes its JSON shape fails SILENTLY: job counts just quietly
drop. This probes a known-good public board per ATS (the `canary` a
fetchable `config.BOARDS` spec names) with the engine's own cheap read
(`Board.alive`: one page, the listing's total where it reports one) and
reports what actually came back.

Two things make the result trustworthy:

  * The probe is the board's own count, no keyword filter: it measures
    the BOARD, not the profile.
  * Failures are classified rather than lumped together. A GitHub runner
    getting a 403 from an anti-bot service is not the same event as your
    parser breaking, and a canary that cries wolf gets ignored.

A canary's `min_jobs` is the floor below which its board reads degraded:
set well under the board's normal size, so ordinary hiring slowdowns do
not cry wolf. A small employer that legitimately empties out is replaced
in its spec rather than given a floor of 0.

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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools._harness import blame, console_utf8              # noqa: E402

console_utf8()

from pydantic import BaseModel                       # noqa: E402

from src.ats.board import BOARDS, spec              # noqa: E402

STATUS_EMOJI = {"ok": "✅", "degraded": "⚠️", "blocked": "🚧", "broken": "❌"}


def check_board(board):
    """Probe one platform's canary board and classify the outcome."""
    canary = board.spec.canary
    ats, name, floor = board.name, canary.name, canary.min_jobs
    buf, started = io.StringIO(), time.monotonic()
    try:
        # The engine prints its diagnostics; capture them to classify.
        with contextlib.redirect_stdout(buf):
            ok, n = board.alive(canary.handle)
        note = " ".join(buf.getvalue().split())
        if not ok and not note:
            note = "the board request failed"
        if ok and n >= floor:
            status, detail = "ok", ""
        elif note:
            status, detail = blame(note), note[:160]
        else:
            # 200 + parsed + zero postings: either a genuinely empty board or
            # a silent shape change. Worth a look, not an alarm.
            status = "degraded"
            detail = f"reachable but returned {n} (floor {floor})"
    except Exception as e:
        note = f"{type(e).__name__}: {e}"
        status = blame(note)
        detail, n = note[:160], 0

    return {"ats": ats, "name": name, "status": status, "jobs": n,
            "detail": detail, "seconds": round(time.monotonic() - started, 1)}


def _leaves(board):
    """(path, value, set) for every key of a board's spec that is not
    itself a model or a list of them (their own keys follow)."""
    for path, value, given, _default in spec.walk(board.spec):
        models = value if isinstance(value, tuple) and value else (value,)
        if not all(isinstance(v, BaseModel) for v in models):
            yield path, value, given


def print_resolved(name):
    """Every key of `name`'s spec with its value, marked set (the spec
    gives it) or default."""
    for path, value, given in _leaves(BOARDS[name]):
        print(f"  {'set    ' if given else 'default'}  {path} = {json.dumps(value)}")


def promotion_candidates(least=3):
    """(path, value, platforms) for each key `least` or more specs set to
    one value other than its default: a default worth promoting. A union's
    `kind` picks a model, so it is left out."""
    seen = {}
    for board in BOARDS.values():
        for path, value, given in _leaves(board):
            if given and not path.endswith(".kind"):
                key = (re.sub(r"\[\d+\]", "[]", path), json.dumps(value))
                seen.setdefault(key, set()).add(board.name)
    return sorted(((p, v, sorted(names)) for (p, v), names in seen.items()
                   if len(names) >= least), key=lambda c: (-len(c[2]), c[0]))


def render_markdown(results, checked_at):
    ok = sum(r["status"] == "ok" for r in results)
    lines = [
        "# Board health",
        "",
        f"_{ok}/{len(results)} platforms healthy · checked "
        f"{checked_at} · [how this works]"
        "(tools/check_boards.py)_",
        "",
        "One cheap read per platform against a public sample board: the "
        "board's own posting count, whatever any search profile wants.",
        "",
        "| | Platform | Sample board | Postings | Detail |",
        "|---|---|---|---:|---|",
    ]
    lines += [f"| {STATUS_EMOJI.get(r['status'], '?')} | `{r['ats']}` "
              f"| {r['name']} | {r['jobs']} | {r['detail'] or r['status']} |"
              for r in sorted(results, key=lambda r: (r["status"] != "ok", r["ats"]))]
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
    ap.add_argument("--resolved", metavar="PLATFORM", choices=sorted(BOARDS),
                    help="print the platform's spec resolved, each value set or default")
    ap.add_argument("--promote", action="store_true",
                    help="list keys 3+ specs set to one non-default value")
    args = ap.parse_args()
    if args.resolved:
        print_resolved(args.resolved)
        return 0
    if args.promote:
        for path, value, names in promotion_candidates():
            print(f"  {len(names):2}  {path} = {value}  ({', '.join(names)})")
        return 0

    results = []
    for board in (b for b in BOARDS.values() if b.fetchable and b.spec.canary):
        r = check_board(board)
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
