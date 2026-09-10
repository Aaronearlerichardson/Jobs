"""
The review queue: where every automated discovery path parks the companies
it guessed, until a person confirms or rejects them. Split out of core.store
on 2026-09-10; core.store re-exports every public name here, so callers keep
saying ``store.confirm_company``.

This module must not import core.store at module level: store imports it
at load time to re-export it, and a module-level import back would make
whichever side loads first fail. The one row helper a body needs is
imported inside the function; doctests import what they use.
"""

import re
from datetime import datetime

import tags

from .schema import connect  # noqa: F401  (the doctests open stores)


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
# instead of onto the roster: an `active = 0` row carrying tags.PENDING,
# invisible to every crawl (they all read get_companies(active_only=True)),
# until a person confirms or rejects it.


def _name_key(name):
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


def mark_pending(row):
    """A company-row dict rewritten as a REVIEW CANDIDATE: inactive, and
    carrying the pending-review scope tag.

    The contract every automated discovery path writes new companies under.

    >>> sorted(mark_pending({"name": "Acme", "active": 1}).items())
    [('active', 0), ('name', 'Acme'), ('tags', 'pending-review')]

    Scope tags already on the row survive, so confirming it leaves a company
    the crawl knows how to fetch:

    >>> mark_pending({"name": "Acme", "tags": "local"})["tags"]
    'local,pending-review'
    """
    return {**row, "active": 0,
            "tags": tags.join(tags.parse(row.get("tags")) | {tags.PENDING})}


def is_confirmed_company(conn, name):
    """True when the roster already holds a REVIEWED company under `name`: a
    row with a board that is not sitting in the review queue.

    Every discovery write asks this first -- a confirmed company is refreshed
    in place, anything else goes (back) to the queue.

    >>> from core.store import upsert_company, company_id_by_name, record_miss
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
        "SELECT ats, tags FROM companies WHERE lower(name)=lower(?)",
        (name,)).fetchone()
    return bool(row and row["ats"] and not tags.has(row["tags"], tags.PENDING))


# What the review UI shows per candidate: who it is, what board was found,
# how much it produces, and where the guess came from.
_PENDING_FIELDS = (
    "id", "name", "ats", "slug", "wd_tenant", "wd_pod", "wd_site",
    "careers_url", "local_job_count", "total_job_count", "mission_tier",
    "mission_score", "mission_reason", "tags", "source", "created_at", "notes",
)


def pending_companies(conn):
    """The review queue: candidates an automated path resolved and nobody has
    ruled on yet, newest first.

    >>> from core.store import upsert_company, record_miss
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
    cols = ", ".join(_PENDING_FIELDS)
    return [dict(r) for r in conn.execute(
        f"SELECT {cols} FROM companies "
        "WHERE (',' || COALESCE(tags,'') || ',') LIKE ? "
        "ORDER BY created_at DESC, id DESC",
        (f"%,{tags.PENDING},%",)).fetchall()]


def confirm_company(conn, cid, active=None):
    """Accept a review candidate onto the roster: the pending tag comes off
    and `active` is written as given (1 = crawl it, 0 = park it).

    The decision itself is the caller's: the shared mission rule
    (core.claude.is_active_mission) applied to the tier already stored on
    the row. A caller that omits `active` gets that rule applied here as a
    fallback, so the store does not depend on the LLM module on any path
    where the caller decided.

    Returns the confirmed row, or None when there is no such company.

    >>> from core.store import (upsert_company, company_id_by_name,
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
    """
    row = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    if active is None:
        from core.claude import is_active_mission
        active = is_active_mission(row["mission_tier"], row["name"])
    kept = tags.parse(row["tags"]) - {tags.PENDING}
    conn.execute(
        "UPDATE companies SET tags=?, active=? WHERE id=?",
        (tags.join(kept), int(active), cid))
    conn.commit()
    from .__init__ import get_company   # not at module level: see module doc
    return get_company(conn, cid)


def reject_company(conn, cid, reason=None):
    """Throw a review candidate away for good: the row and any jobs it
    produced are deleted, and its name is blocklisted so no discovery path
    re-finds it.

    Returns the rejected name, or None when there is no such company.

    >>> from core.store import (upsert_company, company_id_by_name, upsert_job,
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
    """
    row = conn.execute("SELECT name FROM companies WHERE id=?",
                       (cid,)).fetchone()
    if not row:
        return None
    name = row["name"]
    conn.execute("DELETE FROM jobs WHERE company_id=?", (cid,))
    conn.execute("DELETE FROM companies WHERE id=?", (cid,))
    conn.commit()
    block_name(conn, name, reason)
    return name


def block_name(conn, name, reason=None):
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
    conn.commit()
    return key


def blocked_name_keys(conn):
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
