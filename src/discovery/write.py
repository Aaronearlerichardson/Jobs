"""
The single write path every sourcing route goes through: mission-score a
resolved board and write it to the store as a review candidate.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import cast

from src import store
from src import tags as company_tags
from src.ats import coords
from src.ats.board import company as company_fetch
from src.claude import api as claude_api
from src.digest.render import score_text
from src.match.names import name_key
from src.rows import BoardCoords, BoardHit, CompanyIn, CompanyRow
from src.store.companies import BoardPlan


async def _sample_titles(hit: BoardCoords, n: int = 6) -> list[str]:
    """A few job titles from a confirmed board, for mission context. `hit`
    is a resolver hit or a store row.

    Every family samples through board.company.sample_titles. [] when
    nothing could be read.

    Notes:
        Workday had its own hand-built CXS request here until 2026-09-22.
        It lacked the underscore tenant fix, so the roster's two
        hyphenated tenants (Bioventus, United Therapeutics) were
        mission-scored with no titles at all.
    """
    # A hit carries a multi-part handle in `slug` as a tuple; a row in `handle`.
    board = hit if "handle" in hit else coords.from_hit(hit)
    return await company_fetch.sample_titles(board, n)


async def mission_context(board: BoardCoords) -> str:
    """The free-text context `src.claude.score_company_mission` is given for
    a resolved board or roster row (a hit or a store row): a few live posting
    titles, else the board's own address (coords.board_context) -- '' only
    for a board with neither. Every mission-scoring call site builds its
    context here, so no path scores an employer on its name alone while its
    board could have said more.

    Notes:
        A name alone is a poor signal (a medical-imaging vendor scored
        `other` / 0.05 on its name), and boards that list no postings are
        common.
    """
    titles = " | ".join(t for t in await _sample_titles(board) if t)
    return titles or coords.board_context(board)


def _tracked_elsewhere(plan: BoardPlan, name: str | None) -> CompanyRow | None:
    """The roster row `plan` (store.plan_board) updates when it is not named
    `name`: the same board under another name, a duplicate. A same-name match
    is the ordinary re-probe; another board of a tracked employer is a
    sibling.

    Notes:
        Compares the exact NAME, not name_key, because the upsert keys on it:
        "Alpaca Health" on the board spelled "Alpacahealth" once landed as a
        second row.
    """
    row = plan.row
    return row if plan.action == "update" and row and row["name"] != name else None


def board_already_tracked(conn: sqlite3.Connection,
                           row: CompanyIn) -> CompanyRow | None:
    """`_tracked_elsewhere` of the plan for `row`."""
    return _tracked_elsewhere(store.plan_board(conn, row), row.get("name"))


def report_dup_board(name: str, existing: CompanyRow) -> None:
    print(f"    [dup]  {name[:30]:30} same {existing.get('ats') or '?'} board "
          f"as '{existing.get('name')}' - already tracked, not added")


#: An active roster row that has produced jobs: a nonzero total, or a
#: non-empty crawl in the last 14 days (the one parameter).
_PRODUCTIVE = ("COALESCE(active, 0) AND (COALESCE(total_job_count, 0) > 0 "
               "OR COALESCE(last_nonempty_at, '') >= ?)")


def _since_productive() -> str:
    return (datetime.now() - timedelta(days=14)).isoformat()


def _productive_row(conn: sqlite3.Connection, name: str) -> CompanyRow | None:
    """The roster row named `name` if it is productive (_PRODUCTIVE), else
    None."""
    row = conn.execute(f"SELECT * FROM companies_effective WHERE name=? AND {_PRODUCTIVE}",
                       (name, _since_productive())).fetchone()
    return store.as_company(row) if row else None


def _productive_keys(conn: sqlite3.Connection) -> frozenset[str]:
    """The name keys of every productive roster row, and of every other
    name its employer goes by (store.record_alias): discover_local's
    `tracked`.

    Notes:
        Names only, until 2026-10-08: "CSL Seqirus", "ICON" and 14 more
        whose boards were tracked as "CSL", "ICON plc", ... were resolved
        again on every run, the JS majors among them at up to 60 s each.
    """
    return frozenset(name_key(r[0]) for r in conn.execute(
        "SELECT name FROM companies_effective WHERE employer_id IN "
        f"(SELECT employer_id FROM companies_effective WHERE {_PRODUCTIVE}) "
        f"UNION SELECT name FROM companies_effective WHERE {_PRODUCTIVE}",
        (_since_productive(),) * 2))


async def _score_hit(hit: BoardHit) -> tuple[str | None, float | None, str]:
    """(tier, score, reason) for a resolved board: its mission_context as
    the domain context for src.claude.score_company_mission."""
    return await claude_api.score_company_mission(hit["name"], await mission_context(hit))


async def score_and_upsert(db: store.Writer, hit: BoardHit, source: str,
                           include_missions: list[str] | None = None,
                           tags: str | None = None,
                           scored: tuple[str | None, float | None, str] | None = None,
                           extra: CompanyIn | None = None
                           ) -> tuple[CompanyRow | CompanyIn, int, bool] | None:
    """Mission-score a resolved board and write it to the store (`db`, a
    store.Writer) as a review candidate: the one write path behind every
    automated add surface.

    `hit` is a resolver result: {name, ats, slug, nc, count} plus an optional
    careers_url (`slug` is the (tenant, pod, site) triple for Workday, None
    for a custom board). Returns (row, active, pending): the row as written,
    whether a reviewer's confirmation would activate it
    (src.claude.is_active_mission), and whether it went to the review queue;
    or None when the board is already on the roster under ANOTHER name
    (_tracked_elsewhere). The dedup runs before the mission call, so a
    duplicate costs no LLM request; a caller that scored concurrently
    (populate_companies) passes the result as `scored`.

    A name whose roster row is active and has produced jobs (_productive_row)
    keeps its board, `active` and verdict; the same board only refreshes the
    counts. The rest follows store.plan_board: a different board of an
    employer with a verdict is added beside it as a sibling (store.add_board)
    and a board of an employer whose own is gone replaces it, each with the
    employer's verdict and no score; only a board whose plan `needs_score`
    pays for one.

    >>> import asyncio
    >>> from src.store import Writer, connect, upsert_company
    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "Fortrea", "ats": "workday",
    ...     "handle": "fortrea|1|Fortrea",
    ...     "active": 1, "total_job_count": 350, "mission_tier": "core",
    ...     "mission_score": 0.9})
    >>> async def add():
    ...     async with Writer(conn) as db:
    ...         return await score_and_upsert(db, {"name": "Fortrea",
    ...             "ats": "phenom", "slug": "careers.fortrea.com", "nc": 24,
    ...             "count": 24}, "local_sourcing")
    >>> asyncio.run(add())[1:]  # doctest: +ELLIPSIS
        [sibling] Fortrea: phenom board added beside 'Fortrea'
    (1, False)
    >>> [r[:] for r in conn.execute("SELECT name, ats, active, total_job_count "
    ...                             "FROM companies ORDER BY id")]
    [('Fortrea', 'workday', 1, 350), ('Fortrea (phenom)', 'phenom', 1, 24)]

    The row is inactive and review-pending unless the store has
    already confirmed the name (src.store.is_confirmed_company). `tags`
    defaults to the local scope tag when the board has local jobs; a caller
    with another reason to call the company local (ats_dork's HQ signal)
    passes it. `extra` is further columns the caller owns (apply_to_store's
    [VERIFY] notes).

    Notes:
        Replaced four drifted copies (populate_companies, resolve_leads,
        paste_ingest.add_names, ats_dork.harvest_urls); add_board is not a
        fifth (a URL-registered board is written active, with no total
        count), and ingest.add_manual_job and repair.reresolve_misses still
        differ.
    """
    settled, result, held = await db.run(_settled_board, hit, source, tags, extra)
    if settled:
        return result
    return await db.run(_write_candidate, hit,
                        held or (scored if scored is not None else await _score_hit(hit)),
                        source, include_missions, tags, extra)


def _hit_stamp(hit: BoardHit, source: str, tags: str | None,
               extra: CompanyIn | None) -> CompanyIn:
    """The columns of every board written from a resolver `hit`: counts, scope
    tag (`tags`, else local when it has local jobs), `source`, probe time,
    then `extra`."""
    nc = hit.get("nc") or 0
    return cast(CompanyIn, {
        "local_job_count": nc, "total_job_count": hit.get("count"),
        "tags": (company_tags.LOCAL if nc else None) if tags is None else tags,
        "source": source, "last_probed": datetime.now().isoformat(), **(extra or {})})


def _settled_board(conn: sqlite3.Connection, hit: BoardHit, source: str,
                   tags: str | None, extra: CompanyIn | None
                   ) -> tuple[bool, tuple[CompanyRow | CompanyIn, int, bool] | None,
                              tuple[str | None, float | None, str] | None]:
    """(True, score_and_upsert's answer, None) when the roster settles `hit`
    without a score (a duplicate board, a productive row kept, a sibling of a
    scored employer), else (False, None, the verdict the plan inherits)."""
    name = hit["name"]
    row = coords.from_hit(hit, name=name)
    plan = store.plan_board(conn, row)
    dup = _tracked_elsewhere(plan, name)
    if dup:
        report_dup_board(name, dup)
        return True, None, None
    primary = plan.row
    kept = _productive_row(conn, name) if plan.action == "update" else None
    if kept:
        store.upsert_company(conn, {"name": name,
                              "local_job_count": hit.get("nc") or 0,
                              "total_job_count": hit.get("count")})
        return True, (kept, kept["active"] or 0, False), None
    # A working board is never re-pointed: another board of a scored employer
    # joins it as a sibling, inheriting the verdict, so it pays for no score.
    if plan.action != "sibling" or plan.needs_score or primary is None:
        return False, None, plan.verdict
    row.update(_hit_stamp(hit, source, tags, extra))
    sid, _ = store.add_board(conn, row)
    sibling = cast(CompanyRow, store.get_company(conn, sid))
    print(f"    [sibling] {name}: {row['ats']} board added beside '{primary['name']}'")
    return True, (sibling, sibling["active"] or 0,
                  sibling["review"] == "pending"), None


def _write_candidate(conn: sqlite3.Connection, hit: BoardHit,
                     scored: tuple[str | None, float | None, str], source: str,
                     include_missions: list[str] | None, tags: str | None,
                     extra: CompanyIn | None) -> tuple[CompanyIn, int, bool]:
    """score_and_upsert's write of a scored board (`scored`, its tier,
    score and reason); returns (row, active, pending)."""
    name = hit["name"]
    row = coords.from_hit(hit, name=name)
    tier, score, reason = scored
    # Shared activation rule (src.claude.is_active_mission): active tiers,
    # an UNAVAILABLE (None) score, or a multi-division conglomerate whose
    # subdivisions are filtered at crawl time.
    active = claude_api.is_active_mission(tier, name, include_missions)
    row.update({"mission_tier": tier, "mission_score": score,
                "mission_reason": reason, "active": active})
    row.update(_hit_stamp(hit, source, tags, extra))
    # Nothing an automated pass finds joins the roster by itself: a name
    # the store has never confirmed lands in the review queue
    # (src.store.mark_pending) for a person to accept or reject.
    pending = not store.is_confirmed_company(conn, name)
    if pending:
        row = store.mark_pending(row)
    store.add_board(conn, row)
    return row, active, pending


def _print_scored(name: str, row: CompanyRow | CompanyIn, flag: str) -> None:
    """One populate_companies line: `row`'s mission verdict and `flag`."""
    print(f"    {name:30} {str(row.get('mission_tier')):20} "
          f"{score_text(row.get('mission_score'))}  [{flag}]  "
          f"({row.get('mission_reason')})")
