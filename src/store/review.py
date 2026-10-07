"""
The review queue: where every automated discovery path parks the companies
it guessed, until a person confirms or rejects them. Split out of src.store
on 2026-09-10; src.store re-exports every public name here, so callers keep
saying ``store.confirm_company``.

This module must not import a store sibling at module level: store's
__init__ imports all of them to re-export, and companies.py reaches
back here for the rejection blocklist. Both directions are function-
local, so neither module depends on the other at load time and the
package may import them in any order. Doctests import what they use.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from src import config
from src.rows import CompanyIn, CompanyRow
from .employers import clear_pending
from .schema import _commit, connect, sql_function  # noqa: F401  (connect: the doctests open stores)


# --------------------------------------------------------------------------- #
#  Review queue                                                                #
# --------------------------------------------------------------------------- #
#
# Automated discovery guesses, and the guesses were bad. One pasted page
# produced 15 names that were never employers ("Oncology", "Job Location",
# "Who You Are"); the resolver spent about a thousand HTTP requests on them
# and turned four into ACTIVE roster rows with real boards. Verifying that a
# board exists at a guessed domain proves a board exists -- never that the
# NAME was an employer. So every automated path writes its candidates here
# instead of onto the roster: an `active = 0` row with `review = 'pending'`,
# invisible to every crawl (they all read get_companies(active_only=True)),
# until a person confirms or rejects it.
#
# The state is the EMPLOYER's (employers.review; migration 0005): confirming or
# rejecting a candidate rules on the whole employer and all its boards. A board
# of a vetted employer that is itself awaiting review carries its own state
# (companies.review), and the same calls then rule on that board only. Readers
# ask the companies_effective view.


@sql_function("name_key", 1)
def _name_key(name: str | None) -> str:
    """Normalized comparison key for a company name: [a-z0-9] only.

    The key discovery already compares names by (local_sourcing's
    `_NONALNUM_RE`, snowball's `_norm_key`, config's name blocklist), so one
    rejected spelling blocks the others:

    >>> _name_key("Iris Diagnostics, Inc.")
    'irisdiagnosticsinc'
    >>> _name_key(" Foo-Bar!! ") == _name_key("foobar")
    True
    >>> _name_key(None)
    ''
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def mark_pending(row: CompanyIn) -> CompanyIn:
    """A company-row dict rewritten as a REVIEW CANDIDATE: inactive, and
    `review` pending.

    The contract every automated discovery path writes new companies under.

    >>> sorted(mark_pending({"name": "Acme", "active": 1}).items())
    [('active', 0), ('name', 'Acme'), ('review', 'pending')]

    Scope tags already on the row survive, so confirming it leaves a company
    the crawl knows how to fetch:

    >>> mark_pending({"name": "Acme", "tags": "local"})["tags"]
    'local'
    """
    return {**row, "active": 0, "review": "pending"}


def is_confirmed_company(conn: sqlite3.Connection, name: str) -> bool:
    """True when the roster already holds a REVIEWED company under `name`: a
    row with a board that is not sitting in the review queue.

    Every discovery write asks this first -- a confirmed company is refreshed
    in place, anything else goes (back) to the queue.

    >>> from src.store import upsert_company, company_id_by_name, record_miss
    >>> conn = connect(":memory:")
    >>> is_confirmed_company(conn, "Acme")
    False
    >>> _ = upsert_company(conn, mark_pending(
    ...     {"name": "Acme", "ats": "lever", "slug": "acme"}))
    >>> is_confirmed_company(conn, "Acme")
    False

    Confirming it makes it one, and so does any pre-queue row that already
    carried a board:

    >>> _ = confirm_company(conn, company_id_by_name(conn, "Acme"))
    >>> is_confirmed_company(conn, "Acme")
    True

    A boardless lead or miss row is not a company yet, whatever its tags:

    >>> _ = record_miss(conn, "Zeta", "no-board-found")
    >>> is_confirmed_company(conn, "Zeta")
    False
    """
    row = conn.execute(
        "SELECT ats, review FROM companies_effective WHERE lower(name)=lower(?)",
        (name,)).fetchone()
    return bool(row and row["ats"] and row["review"] != "pending")


def pending_companies(conn: sqlite3.Connection) -> list[CompanyRow]:
    """The review queue: candidates an automated path resolved and nobody has
    ruled on yet, newest first.

    >>> from src.store import upsert_company, record_miss
    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, mark_pending(
    ...     {"name": "First", "ats": "lever", "slug": "first"}))
    >>> _ = upsert_company(conn, mark_pending(
    ...     {"name": "Second", "ats": "ashby", "slug": "second"}))
    >>> [c["name"] for c in pending_companies(conn)]
    ['Second', 'First']

    Only tagged rows -- an ordinary inactive row (a miss, a boardless
    capture lead) is not a review candidate:

    >>> _ = record_miss(conn, "Missed", "no-board-found")
    >>> [c["name"] for c in pending_companies(conn)]
    ['Second', 'First']
    """
    from .companies import as_company  # not at module level: see module doc
    return [as_company(r) for r in conn.execute(
        "SELECT * FROM companies_effective WHERE review = 'pending' "
        "ORDER BY created_at DESC, id DESC").fetchall()]


def confirm_company(conn: sqlite3.Connection, cid: int,
                    active: int | None = None) -> CompanyRow | None:
    """Accept a review candidate onto the roster: its employer (and so every
    board of it) leaves the queue, or just this board when only the board was
    pending. `active` is written as given to this board (1 = crawl it, 0 =
    park it); the employer's other boards get the mission rule's answer
    on their own verdict, and a dead board stays as it is.

    The decision itself is the caller's: the shared mission rule
    (config.is_active_mission) applied to the tier already stored on the
    row. A caller that omits `active` gets that rule applied here as a
    fallback. That fallback used to reach UP into src/claude for the rule,
    which made the store depend on the LLM layer; the rule is config
    policy and lives there now, so this is a downward call like every
    other import in this package.

    Returns the confirmed row, or None when there is no such company.

    >>> from src.store import (upsert_company, company_id_by_name,
    ...                         crawlable_companies)
    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, mark_pending(
    ...     {"name": "Acme", "ats": "lever", "slug": "acme", "tags": "local"}))
    >>> row = confirm_company(conn, company_id_by_name(conn, "Acme"), active=1)
    >>> row["tags"], row["active"]
    ('local', 1)

    The crawl picks it up from that moment; it could not see it before:

    >>> [c["name"] for c in crawlable_companies(conn)]
    ['Acme']

    An explicit verdict is written as-is, whatever the row's tier:

    >>> _ = upsert_company(conn, mark_pending({"name": "Parked"}))
    >>> confirm_company(conn, company_id_by_name(conn, "Parked"), active=0)["active"]
    0

    >>> confirm_company(conn, 9999) is None
    True

    Confirming one board of a pending employer rules on all of them:

    >>> from src.store import add_board
    >>> _ = add_board(conn, mark_pending({"name": "Duo", "ats": "lever", "slug": "d"}))
    >>> _ = add_board(conn, {"name": "Duo", "ats": "ashby", "slug": "d2"})
    >>> sorted(c["name"] for c in pending_companies(conn))
    ['Duo', 'Duo (ashby)']
    >>> _ = confirm_company(conn, company_id_by_name(conn, "Duo (ashby)"))
    >>> pending_companies(conn)
    []
    """
    row = conn.execute("SELECT * FROM companies_effective WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    held = conn.execute("SELECT review FROM employers WHERE id=?", (row["employer_id"],)).fetchone()
    whole = bool(held and held[0] == "pending")
    clear_pending(conn, cid, row["employer_id"], whole=whole)
    boards = conn.execute(
        "SELECT id, name, mission_tier, miss_reason FROM companies_effective "
        "WHERE id=? OR (? AND employer_id=?)", (cid, whole, row["employer_id"])).fetchall()
    for b in boards:
        if b["id"] == cid and active is not None:
            on = active
        elif b["id"] != cid and b["miss_reason"]:
            continue
        else:
            on = config.is_active_mission(b["mission_tier"], b["name"])
        conn.execute("UPDATE companies SET active=? WHERE id=?", (on, b["id"]))
    _commit(conn)
    from .companies import get_company  # not at module level: see module doc
    return get_company(conn, cid)


def reject_company(conn: sqlite3.Connection, cid: int, reason: str | None = None) -> str | None:
    """Throw a review candidate away for good: the row and any jobs it
    produced are deleted, and its name is blocklisted so no discovery path
    re-finds it. A pending EMPLOYER goes with all its boards (and their names
    and its own are blocked); a pending board of a vetted employer goes alone.

    Returns the rejected name, or None when there is no such company.

    >>> from src.store import (upsert_company, company_id_by_name, upsert_job,
    ...                         get_companies, job_exists)
    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, mark_pending(
    ...     {"name": "Job Location", "ats": "lever", "slug": "joblocation"}))
    >>> cid = company_id_by_name(conn, "Job Location")
    >>> _ = upsert_job(conn, {"job_id": "x1", "company_id": cid,
    ...                       "company_name": "Job Location", "title": "T"})
    >>> reject_company(conn, cid, "not a company")
    'Job Location'
    >>> get_companies(conn, active_only=False), job_exists(conn, "x1")
    ([], False)

    Deletion alone would not stick -- the same page re-pasted would resolve
    the same junk again -- so the name is remembered as blocked:

    >>> sorted(blocked_name_keys(conn))
    ['joblocation']

    >>> reject_company(conn, 9999) is None
    True

    A pending employer goes whole; a pending board of a vetted employer goes alone:

    >>> from src.store import add_board, confirm_company
    >>> from src.store.employers import set_pending
    >>> a, _ = add_board(conn, mark_pending({"name": "Duo", "ats": "lever", "slug": "d"}))
    >>> _ = add_board(conn, {"name": "Duo", "ats": "ashby", "slug": "d2"})
    >>> reject_company(conn, a)
    'Duo'
    >>> get_companies(conn, active_only=False)
    []
    >>> a, _ = add_board(conn, {"name": "Tri", "ats": "lever", "slug": "t"})
    >>> b, _ = add_board(conn, mark_pending({"name": "Tri", "ats": "ashby", "slug": "t2"}))
    >>> _ = reject_company(conn, b)
    >>> [c["name"] for c in get_companies(conn, active_only=False)]
    ['Tri']

    A board confirmed on its own outlives its pending employer's rejection:

    >>> a, _ = add_board(conn, mark_pending({"name": "Quad", "ats": "lever", "slug": "q"}))
    >>> b, _ = add_board(conn, {"name": "Quad", "ats": "ashby", "slug": "q2"})
    >>> set_pending(conn, b, False, board=True)
    >>> _ = reject_company(conn, a)
    >>> [c["slug"] for c in get_companies(conn, active_only=False)
    ...  if c["slug"] in ("q", "q2")]
    ['q2']
    """
    row = conn.execute(
        "SELECT c.name, c.employer_id, e.name AS employer, e.review FROM companies c "
        "LEFT JOIN employers e ON e.id = c.employer_id WHERE c.id=?", (cid,)).fetchone()
    if not row:
        return None
    name: str = row["name"]
    if row["review"] == "pending":
        gone = conn.execute("SELECT id, name FROM companies WHERE employer_id=? "
                            "AND (review IS NULL OR review != 'confirmed' OR id=?)",
                            (row["employer_id"], cid)).fetchall()
        names = [r["name"] for r in gone] + [row["employer"]]
    else:
        gone, names = [{"id": cid}], [name]
    ids = [(r["id"],) for r in gone]
    conn.executemany("DELETE FROM jobs WHERE company_id=?", ids)
    conn.executemany("DELETE FROM companies WHERE id=?", ids)
    _commit(conn)
    for n in names:
        block_name(conn, n, reason)
    return name


def block_name(conn: sqlite3.Connection, name: str, reason: str | None = None) -> str | None:
    """Blocklist a company name so no discovery path adds it again. Returns
    its normalized key.

    >>> conn = connect(":memory:")
    >>> block_name(conn, "Who You Are", "JD section header")
    'whoyouare'

    Re-blocking another spelling updates the one row instead of growing a
    second -- the blocklist is keyed by the normalized name:

    >>> block_name(conn, "who you are!", "seen again")
    'whoyouare'
    >>> sorted(blocked_name_keys(conn))
    ['whoyouare']

    A name with nothing to key on is not blockable:

    >>> block_name(conn, "  ") is None
    True
    """
    key = _name_key(name)
    if not key:
        return None
    conn.execute(
        "INSERT INTO name_blocklist (key, name, reason, added_at) "
        "VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
        "name=excluded.name, reason=excluded.reason, added_at=excluded.added_at",
        (key, name, reason, datetime.now().isoformat()))
    _commit(conn)
    return key


def blocked_name_keys(conn: sqlite3.Connection) -> set[str]:
    """Every blocklisted name key -- the set a paste is filtered against.

    >>> conn = connect(":memory:")
    >>> blocked_name_keys(conn) == set()
    True
    >>> _ = block_name(conn, "Oncology")
    >>> blocked_name_keys(conn) == {"oncology"}
    True
    """
    return {r["key"] for r in
            conn.execute("SELECT key FROM name_blocklist").fetchall()}
