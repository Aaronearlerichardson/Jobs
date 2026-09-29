"""Job-id migration: rows stored under an id rule a board spec has since
changed."""

from __future__ import annotations

import json
import sqlite3
from typing import cast

from src.config import TrackDict
from src import store
from src.ats.board import board_for
from src.ops.maintenance import track_store


def rekey_jobs(ats: str, commit: bool = False, t: TrackDict | None = None,
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
        companies = {r["id"]: store.as_company(r) for r in conn.execute(
            "SELECT * FROM companies WHERE ats=?", (ats,))}
        ph = ",".join("?" for _ in companies)
        rows = [dict(r) for r in conn.execute(
            f"SELECT id, job_id, company_id, url FROM jobs "
            f"WHERE company_id IN ({ph}) ORDER BY id", tuple(companies))] if companies else []
        buckets: dict[str, list[tuple[str, str | None]]] = {b: [] for b in rekey_buckets}
        new_ids: dict[int, str | None] = {}
        for r in rows:
            handle = board.handle(companies[r["company_id"]])
            new_ids[r["id"]] = (board.row_id(handle, r["url"]) if handle else None) or None
        # The board spec derives each row's new id (Python); which bucket it
        # lands in is one query. A row's HOLDER is the stored row that
        # already has the new id, else the first row of this batch to claim
        # it (which is the one that becomes a "rekey").
        verdicts = conn.execute("""
            WITH plan AS (
              SELECT r.id, r.job_id, r.company_id, r.url, r.title, p.new_id, d.id AS did
              FROM (SELECT json_extract(value, '$[0]') AS id,
                           json_extract(value, '$[1]') AS new_id
                    FROM json_each(?)) p
              JOIN jobs r ON r.id = p.id
              LEFT JOIN jobs d ON d.job_id = p.new_id
            ), claimed AS (
              SELECT *, FIRST_VALUE(id) OVER first_claim AS claimant FROM plan
              WINDOW first_claim AS (PARTITION BY new_id, did IS NULL ORDER BY id)
            )
            SELECT c.id, c.job_id, c.new_id, h.id AS holder,
                   CASE WHEN c.new_id IS NULL THEN 'unresolvable'
                        WHEN c.new_id = c.job_id THEN 'unchanged'
                        WHEN h.id IS NULL THEN 'rekey'
                        WHEN h.company_id IS NOT c.company_id THEN 'cross-tenant'
                        WHEN same_posting(h.url, h.title, c.url, c.title) THEN 'merge'
                        ELSE 'conflict' END AS kind
            FROM claimed c
            LEFT JOIN jobs h ON h.id = COALESCE(c.did, NULLIF(c.claimant, c.id))
            ORDER BY c.id""",
            (json.dumps([[i, n] for i, n in new_ids.items()]),)).fetchall()
        # A holder can have several rows merging into it, and merge_jobs may
        # delete the holder itself (its survivor is the best-ranked row, not
        # the holder), so each holder's rows go through ONE merge.
        rekeys: list[tuple[str, int]] = []
        merges: dict[int, list[int]] = {}
        for v in verdicts:
            buckets[v["kind"]].append((v["job_id"], v["new_id"]))
            if v["kind"] == "rekey":
                rekeys.append((v["new_id"], v["id"]))
            elif v["kind"] == "merge":
                merges.setdefault(v["holder"], []).append(v["id"])
        if commit:
            with store.batch(conn):
                conn.executemany("UPDATE jobs SET job_id=? WHERE id=?", rekeys)
                for holder, members in merges.items():
                    store.merge_jobs(conn, [holder, *members], cast(str, new_ids[members[0]]))
    print(f"  {ats}: {len(rows)} stored row(s) under {len(companies)} compan(ies)")
    for b in rekey_buckets:
        print(f"    {b:13} {len(buckets[b])}")
        for old, new in buckets[b][:5] if b != "unchanged" else []:
            print(f"      {old!r} -> {new}")
    print("  applied." if commit else "  preview: nothing written.")
    return {b: len(v) for b, v in buckets.items()}
