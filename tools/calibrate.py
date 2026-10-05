#!/usr/bin/env python3
"""Does the fit score agree with what you actually did?

    python tools/calibrate.py                 # offline: stored scores
    python tools/calibrate.py --rescore       # also score each decided job again
                                              # with the current prompt (paid)
    python tools/calibrate.py --thresholds 0.2,0.3,0.4,0.5

Reads every job you marked (applied, interviewing, rejected, dismissed) and
shows where its score sits against the digest threshold: how many of the
jobs you pursued the threshold would have shown you, how many you
dismissed it would have shown, and how many open rows it lets through (the
reading you would have to do). Run it after any change to the scoring
prompt, the rubric or `digest_min_fit`, before trusting the new numbers.

Two caveats. The sample is small and every row in it was already shown to
you, so it can only miss jobs the score buried, never ones it hid before you
ever saw them; those are what `--rescore` on dropped rows would find. And
the scoring prompt carries your newest decisions as examples
(claude.fit.disposition_examples_block), so `--rescore` flatters the rows
those examples name.

Read-only. `--rescore` makes one Claude call per decided job.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import aclosing
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, runstate, store  # noqa: E402
from src.net.parallel import fan_out  # noqa: E402

#: How a disposition groups: what you went after, and what you passed on.
CLASS = {"interviewing": "interviewed", "dismissed": "dismissed"}


def sweep(scores: dict[str, list[float]], open_scores: Sequence[float],
          thresholds: Sequence[float]) -> list[tuple[float, float, float, float, int]]:
    """One row per threshold: (threshold, share of pursued rows at or above
    it, share of interviewed, share of dismissed, open rows at or above it).

    >>> sweep({"pursued": [0.2, 0.6], "interviewed": [0.5], "dismissed": [0.1, 0.45]},
    ...       [0.1, 0.5, 0.9], [0.4])
    [(0.4, 0.5, 1.0, 0.5, 2)]
    """
    def share(xs: Sequence[float], t: float) -> float:
        return sum(x >= t for x in xs) / len(xs) if xs else float("nan")
    return [(t, share(scores.get("pursued", []), t), share(scores.get("interviewed", []), t),
             share(scores.get("dismissed", []), t), sum(x >= t for x in open_scores))
            for t in thresholds]


def decided(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT job_id, company_name, title, location, description, resume_fit_score, "
        "fit_gates, fit_model, disposition, disposition_note "
        "FROM jobs WHERE disposition IN (SELECT value FROM json_each(?)) "
        "ORDER BY resume_fit_score DESC",
        (json.dumps(store.RANKING_EXCLUDED_DISPOSITIONS),))]


async def _rescore(rows: Sequence[dict[str, Any]]) -> dict[str, float | None]:
    from src.claude.fit import score_resume_fit

    async def one(r: dict[str, Any]) -> float | None:
        return (await score_resume_fit(r["title"] or "", r["description"] or "",
                                       location=r["location"] or "")).score
    out: dict[str, float | None] = {}
    async with aclosing(fan_out(rows, one, lambda r: f"rescore {r['company_name']}",
                                with_item=True)) as got:
        async for r, score in got:
            out[r["job_id"]] = score
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rescore", action="store_true",
                    help="score each decided job again with the current prompt (paid)")
    ap.add_argument("--thresholds", default="0.2,0.3,0.4,0.5,0.6",
                    help="comma-separated digest thresholds to compare")
    args = ap.parse_args(argv)
    track = config.UI_TRACKS[config.DEFAULT_TRACK or next(iter(config.UI_TRACKS))]
    conn = store.connect(track.db_path)
    try:
        rows = decided(conn)
        open_scores = [r[0] for r in conn.execute(
            "SELECT resume_fit_score FROM open_jobs WHERE resume_fit_score IS NOT NULL "
            "AND disposition IS NULL")]
    finally:
        conn.close()
    thresholds = [float(x) for x in args.thresholds.split(",")]
    new = runstate.run(_rescore([r for r in rows if r["description"]])) if args.rescore else {}

    print(f"\n  {len(rows)} decided job(s); digest_min_fit is {track.digest_min_fit:.2f}\n")
    print(f"  {'fit':>5} {'now':>5}  {'decision':12} {'company':24} title")
    for r in rows:
        fit = r["resume_fit_score"]
        again = new.get(r["job_id"])
        print(f"  {'-' if fit is None else f'{fit:.2f}':>5} "
              f"{'' if again is None else f'{again:.2f}':>5}  {r['disposition']:12} "
              f"{(r['company_name'] or '')[:24]:24} {(r['title'] or '')[:60]}"
              f"{'  gate:' + r['fit_gates'] if r['fit_gates'] else ''}")

    groups: dict[str, list[float]] = {}
    for r in rows:
        if r["resume_fit_score"] is not None:
            groups.setdefault(CLASS.get(r["disposition"], "pursued"), []).append(r["resume_fit_score"])
    print("\n  median fit: " + ", ".join(f"{k} {median(v):.2f} (n={len(v)})"
                                         for k, v in sorted(groups.items())))
    print(f"\n  {'at/above':>8} {'pursued':>8} {'interviewed':>12} {'dismissed':>10} "
          f"{'open rows':>10}")
    for t, p, i, d, n in sweep(groups, open_scores, thresholds):
        print(f"  {t:>8.2f} {p:>8.0%} {i:>12.0%} {d:>10.0%} {n:>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
