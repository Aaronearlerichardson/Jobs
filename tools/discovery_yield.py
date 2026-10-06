#!/usr/bin/env python3
"""What did discovery put on the roster, and what came of it? Read-only.

    python tools/discovery_yield.py                    # roster, misses, yield
    python tools/discovery_yield.py --since 2026-10-05 # plus rows created since
    python tools/discovery_yield.py --json out.json    # the same, as JSON

Opens <data dir>/jobs.db with a mode=ro URI (no migration, no WAL pragma).
Every `jobs` query is run through EXPLAIN QUERY PLAN first; the plans are in
the JSON and a full table scan is flagged in the text.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import config, tags  # noqa: E402
from tools._harness import open_ro  # noqa: E402

FIT_BAR = 0.40
DIMENSIONS = ("source", "mission_tier", "ats")
FAMILY = "substr(miss_reason, 1, instr(miss_reason || ':', ':') - 1)"
OPEN = "COALESCE(j.status, 'open') != 'closed'"
PENDING_LIKE = f"%,{tags.PENDING},%"
ROSTER_COLS = ("COUNT(*) AS rows, SUM(ats IS NOT NULL) AS boards, "
               "SUM(COALESCE(local_job_count, 0) > 0) AS local, SUM(active = 1) AS active, "
               "SUM((',' || COALESCE(tags, '') || ',') LIKE ?) AS pending")


def query(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = (),
          plans: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Rows of `sql` as dicts; a query over `jobs` logs its plan to `plans` first.

    >>> conn = sqlite3.connect(":memory:")
    >>> conn.row_factory = sqlite3.Row
    >>> _ = conn.execute("CREATE TABLE jobs (a)")
    >>> plans = []
    >>> query(conn, "SELECT COUNT(*) AS n FROM jobs", plans=plans), plans[0]["scan"]
    ([{'n': 0}], True)
    """
    if plans is not None and "jobs" in sql:
        plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]
        plans.append({"sql": " ".join(sql.split()), "plan": plan,
                      "scan": any(p.startswith("SCAN") and " USING" not in p for p in plan)})
    return [dict(r) for r in conn.execute(sql, args)]


def report(conn: sqlite3.Connection, since: str | None = None, fit: float = FIT_BAR) -> dict[str, Any]:
    """Every section of the report as one JSON-compatible dict.

    >>> from src import store
    >>> conn = store.connect(":memory:")
    >>> _ = conn.executemany("INSERT INTO companies (name, ats, source, mission_tier, local_job_count, active,"
    ...                      " created_at, miss_reason) VALUES (?,?,?,?,?,?,?,?)",
    ...     [("A", "lever", "dork", "core-mission", 3, 1, "2026-10-05", None),
    ...      ("B", None, "dork", None, None, 0, "2026-09-01", "no-board-found:wrong-domain"),
    ...      ("C", None, "seed", "adjacent", None, 0, "2026-10-06", "no-board-found")])
    >>> _ = conn.execute("INSERT INTO jobs (job_id, company_id, resume_fit_score, geo_mode) VALUES ('j', 1, .5, 'remote')")
    >>> r = report(conn, since="2026-10-01")
    >>> r["roster"]["total"][0]["rows"], r["roster"]["total"][0]["boards"], r["roster"]["total"][0]["local"]
    (3, 1, 1)
    >>> [(m["family"], m["rows"], m["boardless"]) for m in r["misses"]["family"]]
    [('no-board-found', 2, 2)]
    >>> r["yield"]["by_source"], r["yield"]["by_geo_mode"]
    ([{'key': 'dork', 'companies': 1, 'jobs': 1}], [{'key': 'remote', 'companies': 1, 'jobs': 1}])
    >>> [(s["source"], s["tier"], s["ats"], s["rows"]) for s in r["since"]["groups"]]
    [('dork', 'core-mission', 'lever', 1), ('seed', 'adjacent', None, 1)]
    >>> any(p["scan"] for p in r["plans"])
    False
    """
    plans: list[dict[str, Any]] = []
    roster: dict[str, Any] = {"total": query(conn, f"SELECT {ROSTER_COLS} FROM companies_effective", (PENDING_LIKE,))}
    for dim in DIMENSIONS:
        roster[dim] = query(conn, f"SELECT COALESCE({dim}, '(none)') AS key, {ROSTER_COLS} "
                                  f"FROM companies_effective GROUP BY key ORDER BY rows DESC, key", (PENDING_LIKE,))
    misses = {"family": query(conn, f"SELECT {FAMILY} AS family, COUNT(*) AS rows, SUM(ats IS NULL) AS boardless "
                                    "FROM companies_effective WHERE miss_reason IS NOT NULL GROUP BY family ORDER BY rows DESC"),
              "reason": query(conn, "SELECT miss_reason AS reason, COUNT(*) AS rows, SUM(ats IS NULL) AS boardless "
                                    "FROM companies_effective WHERE miss_reason IS NOT NULL GROUP BY reason ORDER BY rows DESC")}
    yields: dict[str, Any] = {"min_fit": fit}
    for label, col in (("by_source", "c.source"), ("by_geo_mode", "j.geo_mode")):
        yields[label] = query(
            conn, f"SELECT COALESCE({col}, '(none)') AS key, COUNT(DISTINCT j.company_id) AS companies, "
                  f"COUNT(*) AS jobs FROM jobs j JOIN companies_effective c ON c.id = j.company_id "
                  f"WHERE {OPEN} AND j.resume_fit_score >= ? GROUP BY key ORDER BY jobs DESC, key", (fit,), plans)
    out: dict[str, Any] = {"roster": roster, "misses": misses, "yield": yields, "plans": plans}
    if since:
        rows = query(conn, "SELECT name, source, mission_tier AS tier, ats, local_job_count AS local, created_at "
                           "FROM companies_effective WHERE created_at >= ? ORDER BY created_at", (since,))
        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        for r in rows:
            g = groups.setdefault((r["source"], r["tier"], r["ats"]), {
                "source": r["source"], "tier": r["tier"], "ats": r["ats"], "rows": 0, "local": 0})
            g["rows"] += 1
            g["local"] += (r["local"] or 0) > 0
        out["since"] = {"date": since, "rows": rows, "groups": sorted(
            groups.values(), key=lambda g: (-g["rows"], str(g["source"]), str(g["tier"]), str(g["ats"])))}
    return out


def table(rows: list[dict[str, Any]]) -> list[str]:
    """Aligned text rows, header first; empty input gives no lines.

    >>> print("\\n".join(table([{"key": "a", "n": 3}, {"key": "bb", "n": None}])))
    key  n
    a    3
    bb   -
    """
    if not rows:
        return []
    cells = [[str(c) for c in rows[0]]] + [["-" if v is None else str(v) for v in r.values()] for r in rows]
    widths = [max(len(c[i]) for c in cells) for i in range(len(cells[0]))]
    return ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in cells]


def render(rep: dict[str, Any]) -> str:
    """The report as text: one headed table per section."""
    lines: list[str] = []

    def section(title: str, rows: list[dict[str, Any]]) -> None:
        lines.extend(["", f"== {title}", *table(rows)])

    t = rep["roster"]["total"][0]
    lines.append(f"roster: {t['rows']} rows, {t['boards']} with a board, {t['local']} with local jobs, "
                 f"{t['active']} active, {t['pending']} pending-review")
    for dim in DIMENSIONS:
        section(f"roster by {dim}", rep["roster"][dim])
    section("misses by family (boardless = no ats)", rep["misses"]["family"])
    section("misses by reason", rep["misses"]["reason"])
    for label in ("by_source", "by_geo_mode"):
        section(f"open jobs with resume_fit_score >= {rep['yield']['min_fit']:.2f} {label.replace('_', ' ')}",
                rep["yield"][label])
    if "since" in rep:
        s = rep["since"]
        section(f"rows created since {s['date']}: {len(s['rows'])}", s["groups"])
    scans = [p["sql"] for p in rep["plans"] if p["scan"]]
    lines.extend(["", f"query plans: {len(rep['plans'])} jobs queries, {len(scans)} full scans"])
    lines.extend(f"  SCAN: {sql}" for sql in scans)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="also list rows created on or after this date")
    ap.add_argument("--json", metavar="PATH", help="write the report as JSON")
    ap.add_argument("--fit", type=float, default=FIT_BAR, help="yield threshold (default %(default)s)")
    ap.add_argument("--db", metavar="PATH", help="store file (default: the data dir's jobs.db)")
    args = ap.parse_args(argv)
    conn = open_ro(args.db or config.STORE_DB_PATH)
    try:
        rep = report(conn, args.since, args.fit)
    finally:
        conn.close()
    print(render(rep))
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
