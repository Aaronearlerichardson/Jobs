"""Employer-level facts and the per-board overrides of them.

An employer holds the mission verdict, review state and watch flag; a board
(a `companies` row) inherits them and may override them. The
`companies_effective` view alone decides which wins. These functions are the
writers, defaulting to the EMPLOYER so every board inherits. `active`, the
board's own crawl switch, is not one of them (companies.py).

Imports only .schema, so companies.py and review.py may both use it.
"""

from __future__ import annotations

import sqlite3

from .schema import _commit, apply_update

#: The columns of one mission verdict, on an employer and on a board alike.
MISSION_COLS = ("mission_tier", "mission_score", "mission_reason")


def _employer(conn: sqlite3.Connection, company_id: int) -> int:
    """The employer id of board `company_id` (KeyError when there is none)."""
    row = conn.execute("SELECT employer_id FROM companies WHERE id=?", (company_id,)).fetchone()
    if not row or row[0] is None:
        raise KeyError(company_id)
    return int(row[0])


def write_mission(conn: sqlite3.Connection, company_id: int, **facts: object) -> None:
    """Write mission columns (tier, score, reason) given by name onto the
    EMPLOYER of board `company_id`; every board without an override of its
    own reads them.

    >>> from src.store.schema import connect
    >>> from src.store import upsert_company
    >>> conn = connect(":memory:")
    >>> cid = upsert_company(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> write_mission(conn, cid, mission_tier="core", mission_score=0.8)
    >>> conn.execute("SELECT mission_tier, mission_score FROM companies_effective").fetchone()[:]
    ('core', 0.8)
    """
    sets = {k: v for k, v in facts.items() if k in MISSION_COLS}
    if sets:
        conn.execute(f"UPDATE employers SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                     [*sets.values(), _employer(conn, company_id)])


def set_board_mission(conn: sqlite3.Connection, company_id: int, tier: str | None,
                      score: float | None, reason: str | None = None) -> None:
    """Score ONE board as a division of its own, overriding the employer's
    verdict for this board only. Needs a tier or a score (neither reads as
    "inherit").

    >>> from src.store.schema import connect
    >>> from src.store import add_board
    >>> conn = connect(":memory:")
    >>> a, _ = add_board(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> b, _ = add_board(conn, {"name": "Acme", "ats": "workday", "handle": "x|1|s"})
    >>> write_mission(conn, a, mission_tier="core", mission_score=0.9)
    >>> set_board_mission(conn, b, "other", 0.1, "the hospital division")
    >>> [r[:] for r in conn.execute("SELECT id = ?, mission_tier, mission_score "
    ...                             "FROM companies_effective ORDER BY id", (a,))]
    [(1, 'core', 0.9), (0, 'other', 0.1)]
    """
    if tier is None and score is None:
        raise ValueError("a board verdict needs a tier or a score")
    apply_update(conn, "companies", "id", company_id,
                 dict(zip(MISSION_COLS, (tier, score, reason))))


def _set_fact(conn: sqlite3.Connection, company_id: int, col: str, *, board: bool,
              board_value: object, employer_value: object) -> None:
    """Write fact `col` on board `company_id` (`board_value`), or on its
    employer (`employer_value`) and drop the board's own, in one commit."""
    if board:
        apply_update(conn, "companies", "id", company_id, {col: board_value})
        return
    conn.execute(f"UPDATE employers SET {col}=? WHERE id=?",
                 (employer_value, _employer(conn, company_id)))
    conn.execute(f"UPDATE companies SET {col}=NULL WHERE id=?", (company_id,))
    _commit(conn)


def set_watch(conn: sqlite3.Connection, company_id: int, on: bool, *, board: bool = False) -> None:
    """Watch (or stop watching) the employer of board `company_id`, or with
    `board` only this board. An employer-wide call drops the board's own
    override.

    >>> from src.store.schema import connect
    >>> from src.store import add_board
    >>> conn = connect(":memory:")
    >>> a, _ = add_board(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> b, _ = add_board(conn, {"name": "Acme", "ats": "ashby", "slug": "b"})
    >>> set_watch(conn, a, True)
    >>> [r[0] for r in conn.execute("SELECT watch FROM companies_effective")]
    [1, 1]
    >>> set_watch(conn, b, False, board=True)
    >>> [r[0] for r in conn.execute("SELECT watch FROM companies_effective")]
    [1, 0]
    """
    _set_fact(conn, company_id, "watch", board=board, board_value=int(on), employer_value=int(on))


def set_pending(conn: sqlite3.Connection, company_id: int, pending: bool = True, *,
                board: bool | None = None) -> None:
    """Put the employer of board `company_id` into (or out of) the review
    queue; `board=True` marks only this board (a new board of a vetted
    employer awaits its own review). `board=None` means the whole employer,
    except that a pending board of a multi-board employer is its own.

    >>> from src.store.schema import connect
    >>> from src.store import add_board
    >>> conn = connect(":memory:")
    >>> a, _ = add_board(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> b, _ = add_board(conn, {"name": "Acme", "ats": "ashby", "slug": "b"})
    >>> set_pending(conn, b)
    >>> [r[0] for r in conn.execute("SELECT review FROM companies_effective")]
    ['confirmed', 'pending']
    >>> set_pending(conn, a, board=False)
    >>> [r[0] for r in conn.execute("SELECT review FROM companies_effective")]
    ['pending', 'pending']
    """
    if board is None:
        board = pending and conn.execute(
            "SELECT COUNT(*) FROM companies WHERE employer_id=(SELECT employer_id FROM companies "
            "WHERE id=?)", (company_id,)).fetchone()[0] > 1
    _set_fact(conn, company_id, "review", board=bool(board),
              board_value="pending" if pending else "confirmed",
              employer_value="pending" if pending else None)


def apply_facts(conn: sqlite3.Connection, company_id: int, pending: bool, watch: bool) -> None:
    """Make true of board `company_id` the facts a write names (`pending`,
    `watch`), each at the level set_pending / set_watch choose; one already
    effective is left alone, and a write never clears either (upsert_company's
    doctests show it at work)."""
    if not (pending or watch):
        return
    row = conn.execute("SELECT review, watch FROM companies_effective WHERE id=?",
                       (company_id,)).fetchone()
    if watch and not row["watch"]:
        set_watch(conn, company_id, True)
    if pending and row["review"] != "pending":
        set_pending(conn, company_id)


def clear_pending(conn: sqlite3.Connection, company_id: int, employer_id: int, *,
                  whole: bool) -> None:
    """Take a reviewed candidate out of the queue: with `whole`, the employer
    `employer_id` and every board of it, else board `company_id` alone.
    The caller commits (review.confirm_company)."""
    if whole:
        conn.execute("UPDATE employers SET review=NULL WHERE id=?", (employer_id,))
        conn.execute("UPDATE companies SET review=NULL WHERE employer_id=?", (employer_id,))
    else:
        conn.execute("UPDATE companies SET review=NULL WHERE id=?", (company_id,))
