"""Job-id migration: rows stored under an id rule a board spec has since
changed."""

from __future__ import annotations

import sqlite3
from typing import Any, cast

from src import store
from src.ats.board import board_for
from src.ops.maintenance import track_store


def rekey_jobs(ats: str, commit: bool = False, t: dict[str, Any] | None = None,
               conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """PREVIEW (default) or APPLY moving every stored job under an `ats`
    company to the id that board's spec gives it now (`Board.row_id`: the
    company's handle and the posting the row's URL names). Prints each
    bucket's count and five samples; returns {bucket: count}.

    Each row lands in one bucket:
      unchanged     its id already is the spec's
      rekey         the new id is free: the row takes it
      merge         a row of the same company holds it for the same posting
                    (store.same_posting: a harvest ran first); the two
                    become one (store.merge_jobs)
      conflict      a row of the same company holds it for another
                    posting: both kept
      cross-tenant  another company's row holds it: both kept
      unresolvable  the URL names no posting the spec reads (a row an
                    earlier platform stored, a malformed URL): untouched

    Only `commit=True` writes, as one store.batch.

    Notes:
        D11 (2026-09-23): Phenom ids gained their host, "phenom_<reqId>"
        -> "phenom_<host_key>_<reqId>", because tenants on their own
        domains can share requisition numbers. Run it before the first
        harvest under a new rule: upsert_job re-keys a row itself only on
        an exact URL and title match, and never moves its company_id.
    """
    rekey_buckets = ("unchanged", "rekey", "merge", "conflict", "cross-tenant",
                     "unresolvable")
    board = board_for(ats)
    if board is None:
        print(f"  [!] no board spec reads {ats!r} rows")
        return {}
    with track_store(t, conn) as conn:
        companies = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM companies WHERE ats=?", (ats,))}
        ph = ",".join("?" for _ in companies)
        rows = [dict(r) for r in conn.execute(
            f"SELECT id, job_id, company_id, url, title FROM jobs "
            f"WHERE company_id IN ({ph}) ORDER BY id", tuple(companies))] if companies else []
        buckets: dict[str, list[tuple[str, str | None]]] = {b: [] for b in rekey_buckets}
        plan: list[tuple[str, dict[str, Any], str | None, dict[str, Any] | None]] = []
        claimed: dict[str | None, dict[str, Any]] = {}
        for r in rows:
            handle = board.handle(companies[r["company_id"]])
            new_id = board.row_id(handle, r["url"]) if handle else None
            holder = claimed.get(new_id) or (new_id and conn.execute(
                "SELECT id, job_id, company_id, url, title FROM jobs WHERE job_id=?",
                (new_id,)).fetchone())
            holder = dict(holder) if holder else None
            if not new_id:
                kind = "unresolvable"
            elif new_id == r["job_id"]:
                kind = "unchanged"
            elif holder is None:
                kind, claimed[new_id] = "rekey", r
            elif holder["company_id"] != r["company_id"]:
                kind = "cross-tenant"
            else:
                kind = "merge" if store.same_posting(holder, r) else "conflict"
            buckets[kind].append((r["job_id"], new_id))
            if kind in ("rekey", "merge"):
                plan.append((kind, r, new_id, holder))
        if commit:
            with store.batch(conn):
                for kind, r, new_id, holder in plan:
                    if kind == "rekey":
                        conn.execute("UPDATE jobs SET job_id=? WHERE id=?", (new_id, r["id"]))
                    else:
                        # a "merge" always has its holder
                        store.merge_jobs(conn, [cast(dict[str, Any], holder)["id"], r["id"]],
                                         cast(str, new_id))
    print(f"  {ats}: {len(rows)} stored row(s) under {len(companies)} compan(ies)")
    for b in rekey_buckets:
        print(f"    {b:13} {len(buckets[b])}")
        for old, new in buckets[b][:5] if b != "unchanged" else []:
            print(f"      {old!r} -> {new}")
    print("  applied." if commit else "  preview: nothing written.")
    return {b: len(v) for b, v in buckets.items()}
