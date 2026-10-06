"""Per-platform pass health: one `platform_health` row per platform per
crawl/harvest pass (migration 0007). The judgment lives in src.crawl.health."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from .schema import connect  # noqa: F401  (the doctests open stores)

def record_platform_health(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> None:
    """Write one pass's per-platform rows (`pass_at, ats, boards, errors, partial, empty, jobs, fill` keys; `fill` already JSON).

    >>> conn = connect(":memory:")
    >>> record_platform_health(conn, [dict(pass_at="2026-01-01", ats="x", boards=3, errors=1,
    ...                                    partial=0, empty=0, jobs=9, fill="{}")])
    >>> [r["ats"] for r in platform_health_history(conn, "x", 5)]
    ['x']
    """
    cols = ("pass_at", "ats", "boards", "errors", "partial", "empty", "jobs", "fill")
    conn.executemany(
        f"INSERT OR REPLACE INTO platform_health ({', '.join(cols)}) "
        f"VALUES ({', '.join(':' + c for c in cols)})", list(rows))
    conn.commit()


def platform_health_history(conn: sqlite3.Connection, ats: str, n: int,
                            before: str | None = None) -> list[sqlite3.Row]:
    """The latest `n` rows for `ats`, newest first, older than pass `before`.

    >>> conn = connect(":memory:")
    >>> platform_health_history(conn, "x", 3)
    []
    >>> "SEARCH" in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM platform_health "
    ...     "WHERE ats = 'x' AND pass_at < 'z' ORDER BY pass_at DESC LIMIT 3").fetchone()[3]
    True
    """
    return conn.execute(
        "SELECT * FROM platform_health WHERE ats = ? AND pass_at < ? "
        "ORDER BY pass_at DESC LIMIT ?", (ats, before or "9999", n)).fetchall()


def latest_platform_health(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The most recent row of every platform.

    >>> conn = connect(":memory:")
    >>> latest_platform_health(conn)
    []
    """
    return conn.execute(
        "SELECT h.* FROM platform_health h WHERE h.pass_at = "
        "(SELECT MAX(pass_at) FROM platform_health WHERE ats = h.ats) "
        "ORDER BY h.ats").fetchall()
