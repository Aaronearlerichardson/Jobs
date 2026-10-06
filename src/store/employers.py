"""Employer-level facts and the per-board overrides of them.

An employer holds the mission verdict, the review state and the watch flag; a
board (a `companies` row) inherits them and may override them (migration
0005). A reader asks the `companies_effective` view, so which value wins is
decided there and nowhere else. These functions are the writers: each says
which level it writes, and the default is the EMPLOYER, so every board
inherits. Deactivating a dead board is not one of them: `active` is the
board's own crawl switch (companies.py).

Imports only .schema, so companies.py and review.py may both use it.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from src import tags
from .schema import _commit, connect  # noqa: F401  (connect: the doctests open stores)

#: The tag tokens that are employer facts, not board scope.
FACT_TAGS = frozenset({tags.WATCH, tags.PENDING})

_Fact = Literal["mission", "review", "watch"]


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

    >>> from src.store import upsert_company
    >>> conn = connect(":memory:")
    >>> cid = upsert_company(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> write_mission(conn, cid, mission_tier="core", mission_score=0.8)
    >>> conn.execute("SELECT mission_tier, mission_score FROM companies_effective").fetchone()[:]
    ('core', 0.8)
    """
    sets = {k: v for k, v in facts.items() if k in ("mission_tier", "mission_score", "mission_reason")}
    if sets:
        conn.execute(f"UPDATE employers SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                     [*sets.values(), _employer(conn, company_id)])


def set_mission(conn: sqlite3.Connection, company_id: int, tier: str | None,
                score: float | None, reason: str | None = None) -> None:
    """Rescore the employer of board `company_id`: all its boards inherit,
    except those carrying their own verdict (set_board_mission)."""
    write_mission(conn, company_id, mission_tier=tier, mission_score=score, mission_reason=reason)
    _commit(conn)


def set_board_mission(conn: sqlite3.Connection, company_id: int, tier: str | None,
                      score: float | None, reason: str | None = None) -> None:
    """Score ONE board as a division of its own: the verdict overrides the
    employer's for this board only. Needs a tier or a score (a verdict with
    neither reads as "inherit"). clear_board_override undoes it.

    >>> from src.store import add_board
    >>> conn = connect(":memory:")
    >>> a, _ = add_board(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> b, _ = add_board(conn, {"name": "Acme", "ats": "workday", "wd_tenant": "x",
    ...                         "wd_pod": 1, "wd_site": "s"})
    >>> set_mission(conn, a, "core", 0.9)
    >>> set_board_mission(conn, b, "other", 0.1, "the hospital division")
    >>> [r[:] for r in conn.execute("SELECT id = ?, mission_tier, mission_score "
    ...                             "FROM companies_effective ORDER BY id", (a,))]
    [(1, 'core', 0.9), (0, 'other', 0.1)]
    >>> clear_board_override(conn, b)
    >>> [r[0] for r in conn.execute("SELECT mission_tier FROM companies_effective")]
    ['core', 'core']
    """
    if tier is None and score is None:
        raise ValueError("a board verdict needs a tier or a score")
    conn.execute("UPDATE companies SET mission_tier=?, mission_score=?, mission_reason=? WHERE id=?",
                 (tier, score, reason, company_id))
    _commit(conn)


def clear_board_override(conn: sqlite3.Connection, company_id: int, *facts: _Fact) -> None:
    """Drop board `company_id`'s own mission, review and watch (just the
    ones named, when any are), so it inherits its employer's again."""
    cols: dict[_Fact, tuple[str, ...]] = {
        "mission": ("mission_tier", "mission_score", "mission_reason"),
        "review": ("review",), "watch": ("watch",)}
    chosen = [c for f in (facts or tuple(cols)) for c in cols[f]]
    conn.execute(f"UPDATE companies SET {', '.join(f'{c}=NULL' for c in chosen)} WHERE id=?",
                 (company_id,))
    _commit(conn)


def set_watch(conn: sqlite3.Connection, company_id: int, on: bool, *, board: bool = False) -> None:
    """Watch (or stop watching) the employer of board `company_id`; with
    `board`, only this board. An employer-wide call also drops this board's
    own override, so the board ends up as asked.

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
    if board:
        conn.execute("UPDATE companies SET watch=? WHERE id=?", (int(on), company_id))
    else:
        conn.execute("UPDATE employers SET watch=? WHERE id=?", (int(on), _employer(conn, company_id)))
        conn.execute("UPDATE companies SET watch=NULL WHERE id=?", (company_id,))
    _commit(conn)


def set_pending(conn: sqlite3.Connection, company_id: int, pending: bool = True, *,
                board: bool | None = None) -> None:
    """Put the employer of board `company_id` into (or out of) the review
    queue. `board=True` marks just this board (a new board of a vetted
    employer awaits its own review); the default is the whole employer,
    except that a board of a multi-board employer found for review is its
    own (`board=None` picks by that rule when `pending`).

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
    if board:
        conn.execute("UPDATE companies SET review=? WHERE id=?",
                     ("pending" if pending else "confirmed", company_id))
    else:
        conn.execute("UPDATE employers SET review=? WHERE id=?",
                     ("pending" if pending else None, _employer(conn, company_id)))
        conn.execute("UPDATE companies SET review=NULL WHERE id=?", (company_id,))
    _commit(conn)


def apply_tag_facts(conn: sqlite3.Connection, company_id: int, held: set[str]) -> None:
    """Make the fact tags in `held` ({watch, pending-review}) true of board
    `company_id`, writing the level each belongs to (set_watch, set_pending);
    a fact already effective is left alone. What an upsert's `tags` and a
    dedup merge do with the two tokens that are not board scope."""
    held = held & FACT_TAGS
    if not held:
        return
    row = conn.execute("SELECT review, watch FROM companies_effective WHERE id=?",
                       (company_id,)).fetchone()
    if tags.WATCH in held and row and not row["watch"]:
        set_watch(conn, company_id, True)
    if tags.PENDING in held and row and row["review"] != "pending":
        set_pending(conn, company_id)
