#!/usr/bin/env python3
"""What would a lower `remote_mission_floor` let through?

    python tools/screen_remote.py                  # count the candidates, no calls
    python tools/screen_remote.py --pilot 20       # score 20 of them (paid)
    python tools/screen_remote.py --go             # score them all (paid)
    python tools/screen_remote.py --report FILE    # reread a saved screen

The geo gate lets a remote, US-eligible posting through only at a company
you watch or whose mission score reaches the track's `remote_mission_floor`.
Every other such posting is dropped before it is read, so nothing says what
that rule costs. This takes those open postings (triage_status='geo', a
location that reads remote and US, a company below the floor, a body on
file), scores each one with the current fit scorer, and tabulates how many
clear `digest_min_fit` by the company's mission score, so the floor is
chosen from what it would admit rather than from a guess.

Nothing is written to the store: the scores go to
<data dir>/screens/remote-<time>.json, one entry per posting.
Rows with no stored body are counted and left out (reading them needs a
detail fetch). One Claude call per scored posting.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, runstate, store, tags  # noqa: E402
from src.match.locality import is_nc, location_unknown, remote_signal, us_eligible  # noqa: E402
from src.ops.maintenance import _mission_trusted  # noqa: E402


def candidates(conn: Any, floor: float | None) -> tuple[list[dict[str, Any]], int]:
    """(postings to screen, postings left out for want of a body)."""
    companies: dict[int, Any] = {}
    out: list[dict[str, Any]] = []
    bodiless = 0
    for r in conn.execute(
            "SELECT job_id, company_id, company_name, title, location, description "
            "FROM open_jobs WHERE triage_status='geo' AND disposition IS NULL"):
        loc = r["location"]
        if (location_unknown(loc) or is_nc(loc)
                or not (remote_signal(loc) and us_eligible(loc))):
            continue
        if r["company_id"] not in companies:
            row = conn.execute("SELECT * FROM companies WHERE id=?", (r["company_id"],)).fetchone()
            companies[r["company_id"]] = store.as_company(row) if row else None
        co = companies[r["company_id"]]
        if co and (tags.has(co, tags.WATCH) or _mission_trusted(co, floor)):
            continue
        if len((r["description"] or "").strip()) < 200:
            bodiless += 1
            continue
        out.append({"job_id": r["job_id"], "company": r["company_name"], "title": r["title"],
                    "location": loc, "description": r["description"],
                    "mission": co.get("mission_score") if co else None})
    return out, bodiless


def floors(scored: Sequence[dict[str, Any]], bar: float) -> list[tuple[float, int, int, int]]:
    """One row per mission score a floor could sit at, highest first:
    (floor, companies admitted, postings admitted, of those at or above `bar`).

    >>> s = [{"company": "A", "mission": 0.6, "fit": 0.5}, {"company": "A", "mission": 0.6, "fit": 0.1},
    ...      {"company": "B", "mission": 0.5, "fit": 0.45}, {"company": "C", "mission": 0.5, "fit": 0.2}]
    >>> floors(s, 0.4)
    [(0.6, 1, 2, 1), (0.5, 3, 4, 2)]
    """
    out = []
    for f in sorted({x["mission"] for x in scored if x["mission"] is not None}, reverse=True):
        got = [x for x in scored if x["mission"] is not None and x["mission"] >= f]
        out.append((f, len({x["company"] for x in got}), len(got),
                    sum(x["fit"] >= bar for x in got)))
    return out


def report(scored: Sequence[dict[str, Any]], bar: float, floor: float | None) -> None:
    print(f"\n  {len(scored)} posting(s) scored; digest_min_fit {bar:.2f}; "
          f"current remote_mission_floor {floor}\n")
    print(f"  {'floor':>6} {'companies':>10} {'postings':>9} {'clear bar':>10} {'share':>6}")
    for f, nco, n, hit in floors(scored, bar):
        print(f"  {f:>6.2f} {nco:>10} {n:>9} {hit:>10} {hit / n:>6.0%}")
    by_co: dict[str, list[float]] = defaultdict(list)
    for x in scored:
        by_co[x["company"]].append(x["fit"])
    print(f"\n  {'company':28} {'mission':>7} {'n':>4} {'clear':>6} {'best':>5}")
    mission = {x["company"]: x["mission"] for x in scored}
    for co, fits in sorted(by_co.items(), key=lambda kv: -sum(f >= bar for f in kv[1]))[:25]:
        print(f"  {co[:28]:28} {mission[co] if mission[co] is not None else '-':>7} "
              f"{len(fits):>4} {sum(f >= bar for f in fits):>6} {max(fits):>5.2f}")
    print("\n  best postings:")
    for x in sorted(scored, key=lambda x: -x["fit"])[:15]:
        print(f"  {x['fit']:.2f}  {x['company'][:22]:22} {x['title'][:50]}  [{x['location'][:30]}]")


async def screen(rows: Sequence[dict[str, Any]], workers: int) -> list[dict[str, Any]]:
    from src.claude.fit import score_resume_fit
    gate = asyncio.Semaphore(workers)
    done: list[dict[str, Any]] = []

    async def one(x: dict[str, Any]) -> None:
        async with gate:
            res = await score_resume_fit(x["title"] or "", x["description"], location=x["location"] or "")
        if res.score is not None:
            done.append({k: v for k, v in x.items() if k != "description"}
                        | {"fit": res.score, "gates": res.gates, "reason": res.summary()})
    await asyncio.gather(*(one(x) for x in rows))
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pilot", type=int, metavar="N", help="score N postings, spread across companies")
    ap.add_argument("--go", action="store_true", help="score every candidate (paid)")
    ap.add_argument("--report", metavar="FILE", help="reread a saved screen; no calls")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)
    track = config.UI_TRACKS[config.DEFAULT_TRACK or next(iter(config.UI_TRACKS))]
    bar, floor = track.digest_min_fit, track.remote_mission_floor
    if args.report:
        report(json.loads(Path(args.report).read_text(encoding="utf-8")), bar, floor)
        return 0
    conn = store.connect(track.db_path)
    try:
        rows, bodiless = candidates(conn, floor)
    finally:
        conn.close()
    print(f"  {len(rows)} posting(s) at {len({r['company'] for r in rows})} company(ies) to screen; "
          f"{bodiless} more have no stored body")
    if not (args.pilot or args.go):
        print("  --pilot N or --go to score them (one Claude call each)")
        return 0
    from src.claude.api import have_api_key
    if not have_api_key():
        print("  [!] no Anthropic API key")
        return 1
    if args.pilot:
        by_co: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_co[r["company"]].append(r)
        picked = [r for turn in zip_longest(*by_co.values()) for r in turn if r]
        rows = picked[:args.pilot]
    scored = runstate.run(screen(rows, args.workers))
    out = config.DATA_DIR / "screens" / f"remote-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scored, indent=1), encoding="utf-8")
    print(f"  {len(scored)} of {len(rows)} scored; saved {out}")
    report(scored, bar, floor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
