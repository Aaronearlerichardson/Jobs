"""Description backfill: the missing JD text of stored rows, from each
company's own board."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import NotRequired, TypedDict, cast

from src import store
from src.ats.board import company as company_fetch
from src.config import RuntimeTrack
from src.net.parallel import fan_out
from src.ops.maintenance import (_t, board_index, board_match,
                                 group_by_company, track_writer)
from src.rows import CompanyRow, FetchedJob


class StaleRow(TypedDict):
    """A stored row `stale_body_rows` selects (`columns` always names job_id,
    title and url; company_id only when asked)."""
    job_id: str
    title: str | None
    url: str | None
    company_id: NotRequired[int | None]
    desc_checked_at: str | None


def stale_body_rows(conn: sqlite3.Connection, where: str,
                    columns: str = "job_id, title, url", min_len: int = 200,
                    retry_days: int = 3, limit: int | None = None,
                    label: str = "description(s)") -> list[StaleRow]:
    """Stored rows with no usable body yet, minus the ones a recent attempt
    already failed on, and the header line saying so.

    `where` is the extra predicate that picks one backfill's population
    (company-linked rows); the rest -- too short to score,
    not closed, not attempted inside `retry_days` -- is the same question
    every backfill asks. Both of them had written it out, and they had
    already diverged: one read `desc_checked_at` off a sqlite3.Row and the
    other off a dict, which is the sort of difference that survives
    because neither copy is ever read beside the other.

    `retry_days=0` retries everything. The skip count is reported, not
    hidden: "0 of 4 backfilled" with no explanation was undiagnosable from
    the session log.
    """
    cutoff = ((datetime.now() - timedelta(days=retry_days)).isoformat()
              if retry_days else "9999")
    rows = [cast(StaleRow, dict(r)) for r in conn.execute(
        f"SELECT {columns}, desc_checked_at FROM open_jobs "
        f"WHERE {where} "
        "AND length(COALESCE(description,'')) < ?", (min_len,)).fetchall()]
    recent = [r for r in rows if (r.get("desc_checked_at") or "") >= cutoff]
    rows = [r for r in rows if (r.get("desc_checked_at") or "") < cutoff]
    if limit:
        rows = rows[:int(limit)]
    print(f"  backfilling {len(rows)} {label}..."
          + (f" ({len(recent)} skipped: failed in the last {retry_days}d)"
             if recent else ""))
    return rows


def save_body(conn: sqlite3.Connection, job_id: str, text: str | None) -> bool:
    """Keep a fetched body, or stamp the failure. True when a body landed.

    The stamp is not optional bookkeeping: an unstamped failure is
    re-selected (and silently re-counted) by every later run, so a board
    that has stopped answering costs a fetch per row forever. Three of the
    four places that wrote this pair spelled the UPDATE out inline, beside
    a store that already exported both halves.
    """
    if not text:
        store.mark_desc_checked(conn, job_id)
        return False
    store.store_body(conn, job_id, text)
    return True


async def backfill_board_descriptions(max_workers: int = 8, limit: int | None = None,
                                      min_len: int = 200, t: RuntimeTrack | None = None,
                                      retry_days: int = 3) -> int:
    """One-shot: fill in full JD text for stored jobs missing it (any
    company-linked row whose description is shorter than min_len chars —
    the default matches src.claude.fit.MIN_DESC_CHARS), via each company's
    own ATS board. Batched per company so a board with several stale rows is
    fetched once. Safe to re-run: a row that failed within the last
    `retry_days` days is skipped (its desc_checked_at stamp), so reruns
    don't re-fetch every board to fail on the same vanished postings
    (retry_days=0 retries everything).

    Companies are fetched CONCURRENTLY (`max_workers`). A row the board
    pull does not cover is hydrated through its company's engine
    (`company_fetch.hydrate_description`).

    Notes:
        This function once advertised max_workers=8 and walked one company
        at a time. The 2026-09-11 web-UI run (session-20260911-162142) got
        through ~1,878 of 43,600 rows in nineteen minutes, nine of them on
        J&J MedTech alone, and was killed before it finished.

        Company lines now print in COMPLETION order, not roster order —
        `fan_out` yields as boards come back, and the sibling has always
        printed that way. The counts either line adds up to are unchanged.

        Only the fetching runs concurrently. Every jobs write goes through
        the store.Writer, grouped per company in one batch so a 1,000-row
        board takes the write lock once instead of a thousand times.

        The per-row loop stays serial INSIDE each company: those rows share
        one board pull and, when the board doesn't cover them, hit one
        host's detail pages, which is exactly the traffic the fetchers'
        own per-source pacing is written to bound.
    """
    t = _t(t)
    async with track_writer(t) as db:
        rows = await db.run(stale_body_rows, "company_id IS NOT NULL",
                            columns="job_id, title, url, company_id",
                            min_len=min_len, retry_days=retry_days,
                            limit=limit,
                            label="description(s) via company board(s)")
        groups: list[tuple[CompanyRow, list[StaleRow]]] = []
        boardless: list[StaleRow] = []
        for cid, rs in group_by_company(rows).items():
            company = await db.run(store.get_company, cid)
            if not company or not company.get("ats"):
                # No board to fetch IS a failed attempt — stamp these rows too,
                # or they are re-selected (and silently re-counted) every run
                # while never even printing a company line.
                boardless.extend(rs)
                continue
            groups.append((company, rs))
        if boardless:
            await db.batch(_save_bodies, [(r["job_id"], None) for r in boardless])

        async def _bodies(group: tuple[CompanyRow, list[StaleRow]]) -> list[tuple[str, str | None]]:
            """One company's fetching: the batched board pull for the common
            case (one fetch per company), then per-job-URL hydration for the
            rows that pull didn't cover. Boards we can't pull simply yield
            no title matches, so every row falls through to hydration either
            way. Returns [(job_id, description_or_None)] to write."""
            company, rs = group
            index = await board_index(company)
            out: list[tuple[str, str | None]] = []
            for r in rs:
                match = await board_match(index, r["title"])
                desc = match.get("description") if match else None
                if not desc and r.get("url"):
                    # Board didn't cover this row — hydrate from the job's own
                    # detail page (JSON-LD / career-site markup).
                    stub: FetchedJob = {"title": r["title"] or "", "url": r["url"],
                            "ats": company.get("ats"), "description": ""}
                    await company_fetch.hydrate_description(stub, company)
                    desc = stub.get("description")
                out.append((r["job_id"], desc))
            return out

        n = 0
        async for (company, rs), bodies in fan_out(
                groups, _bodies,
                lambda g: f"{g[0]['name']} board backfill",
                max_workers, with_item=True):
            n_matched = await db.batch(_save_bodies, bodies)
            n += n_matched
            print(f"    {company['name']:30} {len(rs):2} stale -> {n_matched:2} matched")
    print(f"  {n} of {len(rows)} description(s) backfilled.")
    return n


def _save_bodies(conn: sqlite3.Connection, bodies: Iterable[tuple[str, str | None]]) -> int:
    """save_body over (job_id, text) pairs; how many bodies landed."""
    return sum(save_body(conn, job_id, text) for job_id, text in bodies)
