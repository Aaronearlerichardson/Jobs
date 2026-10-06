"""Per-platform pass health: the one recorder the crawl and the harvest both
feed, and the judgment that flags a platform breaking across its boards.

A soft-failing fetcher returns [] like an empty board does, and dormancy
treats errors as neutral, so a spec that breaks on every board parks them
quietly. This rolls each pass up per platform (store.platform_health) and
compares its error+empty rate with the platform's own trailing median.
Thresholds: config.PLATFORM_HEALTH.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from src import config
from src import store
from src.ats.board.pager import FILL_FLOORS, fill_rates
from src.rows import FetchedJob


class Tally:
    """One pass's board outcomes, per platform.

    >>> t = Tally()
    >>> t.note("x", 2, {"title": 1.0}, err=None, snap={})
    >>> t.note("x", 0, {}, err=None, snap={"fetch_errors": 1, "incomplete": True})
    >>> t.note("x", 0, {}, err=None, snap={})
    >>> t.note("x", 4, {"title": 0.5}, err=None, snap={"capped": True})
    >>> r = t.rows("p")[0]
    >>> (r["boards"], r["errors"], r["empty"], r["partial"], r["jobs"], r["fill"])
    (4, 1, 1, 1, 6, '{"title": 0.667}')
    """

    def __init__(self) -> None:
        self._n: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._fill: defaultdict[str, defaultdict[str, float]] = defaultdict(
            lambda: defaultdict(float))

    def note(self, ats: str, n: int, fill: Mapping[str, float], *,
             err: object, snap: Mapping[str, Any]) -> None:
        """One board's outcome: `n` rows, their `fill` rates, the fetch's
        exception `err` and net.http.snapshot_info() `snap` (any mapping with
        its keys). A failure with no rows is an error, with rows partial."""
        c = self._n[ats]
        c["boards"] += 1
        c["jobs"] += n
        failed = err is not None or bool(snap.get("incomplete"))
        if not n:
            c["errors" if failed else "empty"] += 1
        elif failed or snap.get("capped"):
            c["partial"] += 1
        weight = snap.get("fill_rows", n)
        c["filled"] += weight
        for k, v in fill.items():
            self._fill[ats][k] += v * weight

    def note_jobs(self, ats: str, jobs: list[FetchedJob] | None, err: object,
                  snap: Mapping[str, Any] | None) -> None:
        """`note` for a board whose rows are in hand; the fill is the
        snapshot's (raw listing rows), else the kept `jobs`'.

        >>> t = Tally()
        >>> t.note_jobs("x", [], None, {"fill": {"title": 0.5}, "fill_rows": 4})
        >>> t.rows("p")[0]["fill"]
        '{"title": 0.5}'
        """
        jobs, snap = jobs or [], snap or {}
        fill = snap.get("fill") or (fill_rates(jobs) if jobs else {})
        self.note(ats, len(jobs), fill, err=err, snap=snap)

    def rows(self, pass_at: str) -> list[dict[str, Any]]:
        """The `platform_health` rows of the pass, `fill` as JSON."""
        return [{"pass_at": pass_at, "ats": ats, "boards": c["boards"], "errors": c["errors"],
                 "partial": c["partial"], "empty": c["empty"], "jobs": c["jobs"],
                 "fill": json.dumps({k: round(v / c["filled"], 3)
                                     for k, v in self._fill[ats].items()} if c["filled"] else {})}
                for ats, c in sorted(self._n.items())]


def floors(ats: str) -> dict[str, float]:
    """`ats`'s fill floors: the defaults over its canary's `min_fill`.

    >>> floors("no-such-platform") == FILL_FLOORS
    True
    """
    canary = config.BOARDS.get(ats, {}).get("canary") or {}
    return FILL_FLOORS | dict(canary.get("min_fill", {}))


def _bad(r: Mapping[str, Any]) -> float:
    return (r["errors"] + r["empty"]) / r["boards"] if r["boards"] else 0.0


def flags(row: Mapping[str, Any], history: list[Mapping[str, Any]],
          policy: Mapping[str, Any] | None = None) -> list[str]:
    """Why `row` (one platform's pass) is unhealthy, given its earlier `history`.

    >>> past = [{"boards": 10, "errors": 0, "empty": 1}] * 3
    >>> row = {"ats": "x", "boards": 10, "errors": 6, "empty": 1, "jobs": 5, "fill": "{}"}
    >>> flags(row, past)
    ['error+empty 70% of 10 boards, was 10%']
    >>> flags(row, past[:2])
    []
    >>> flags({**row, "errors": 0, "fill": '{"title": 0.2}'}, past)
    ['fill title 20% < 98%']
    """
    p = policy or config.PLATFORM_HEALTH
    if row["boards"] < p["min_boards"]:
        return []
    out = []
    base = [_bad(h) for h in history[:p["window"]] if h["boards"] >= p["min_boards"]]
    if len(base) >= p["min_passes"] and _bad(row) - statistics.median(base) >= p["margin"]:
        out.append(f"error+empty {_bad(row):.0%} of {row['boards']} boards, "
                   f"was {statistics.median(base):.0%}")
    if row["jobs"]:
        floor = floors(row["ats"])
        out += [f"fill {k} {v:.0%} < {floor[k]:.0%}"
                for k, v in json.loads(row["fill"]).items() if v < floor.get(k, 0)]
    return out


def record(conn: sqlite3.Connection, tally: Tally, now: datetime | None = None) -> list[str]:
    """Store the pass and return one warning line per flagged platform.

    >>> from src.store import connect
    >>> conn = connect(":memory:")
    >>> for day in (1, 2, 3):
    ...     t = Tally()
    ...     for i in range(8): t.note("x", 3, {}, err=None, snap={})
    ...     _ = record(conn, t, datetime(2026, 1, day))
    >>> t = Tally()
    >>> for i in range(8): t.note("x", 0, {}, err=None, snap={"incomplete": True})
    >>> record(conn, t, datetime(2026, 1, 4))
    ['platform x: error+empty 100% of 8 boards, was 0%']
    >>> alerts(conn)
    ['x: error+empty 100% of 8 boards, was 0%']
    """
    stamp = (now or datetime.now()).isoformat()
    rows = tally.rows(stamp)
    lines = [f"platform {r['ats']}: {why}" for r in rows for why in flags(
        r, [dict(h) for h in store.platform_health_history(
            conn, r["ats"], config.PLATFORM_HEALTH["window"], stamp)])]
    store.record_platform_health(conn, rows)
    return lines


def alerts(conn: sqlite3.Connection) -> list[str]:
    """The flags of every platform's latest pass (the status view)."""
    return [f"{r['ats']}: {why}" for r in store.latest_platform_health(conn)
            for why in flags(dict(r), [dict(h) for h in store.platform_health_history(
                conn, r["ats"], config.PLATFORM_HEALTH["window"], r["pass_at"])])]
