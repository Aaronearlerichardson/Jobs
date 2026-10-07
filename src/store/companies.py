"""The roster: the `companies` table, and everything that decides what
goes in it.

Company rows, the miss log (names that resolved to no board), board
identity and dedup, the roster CRUD every discovery path writes through,
and crawl scheduling (dormancy). The review queue is in review.py; jobs
are in jobs.py and this module never touches them.

A row is ONE BOARD. What an employer decides (mission verdict, review state,
watch) lives on its `employers` row, which a board may override
(employers.py). Readers ask the `companies_effective` view; writers go
through _write_company, which sends those facts to the employer. The board
keeps its coordinates, counts, dormancy, scope tags and `active` (its own
crawl switch; the view also reads a pending board as inactive).

Never imports store/__init__ at load time (that module imports this one).

Notes:
    Split out of store/__init__.py; `dedup_jobs` lives in jobs.py because it
    reads only the jobs table.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import EllipsisType
from typing import Annotated, Literal, NamedTuple, TypedDict, Unpack, cast, get_args

from pydantic import AfterValidator, BeforeValidator, ConfigDict, TypeAdapter

from src import config, tags
from src.runstate import per_run
from src.match.names import name_key as _name_key
from src.net.util import dig
from src.rows import BoardCoords, CompanyIn, CompanyRow, HandleColumn
from .employers import MISSION_COLS, apply_facts, write_mission
from .schema import (_commit, apply_update, batch,  # noqa: F401 (doctests)
                     connect, dedup_groups)


def as_company(row: sqlite3.Row) -> CompanyRow:
    """A `SELECT *` companies_effective row as a CompanyRow: every column,
    which the total type promises, its `tags` in the canonical order.

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "A", "tags": "sweep,nc_local"})
    >>> as_company(conn.execute("SELECT * FROM companies_effective").fetchone())["tags"]
    'local,sweep'
    """
    out = dict(row)
    out["tags"] = tags.join(tags.parse(out.get("tags")))
    return cast(CompanyRow, out)


# --------------------------------------------------------------------------- #
#  Companies                                                                   #
# --------------------------------------------------------------------------- #

class BoardPlan(NamedTuple):
    """What add_board will do with a board. `action`: "new" (a company, and its
    employer if `employer_id` is None), "update" (already `row`'s board),
    "replace" (`row`'s board is gone: point it here) or "sibling" (another
    board of `row`'s employer; `row` is its primary)."""
    action: Literal["new", "update", "replace", "sibling"]
    row: CompanyRow | None = None
    employer_id: int | None = None
    #: The employer's (tier, score, reason) a "replace"/"sibling" inherits, or None.
    verdict: tuple[str | None, float | None, str] | None = None

    @property
    def needs_score(self) -> bool:
        """True when the board needs a mission score of its own (no inherited `verdict`)."""
        return self.verdict is None


# The columns that name a board (the handle columns of every ATS spec, plus
# the ats itself).
_COORD_COLUMNS = frozenset({"ats", *get_args(HandleColumn)})


def _board_gone(row: CompanyRow) -> bool:
    """True when `row` has no board worth keeping: none named, a recorded miss
    against it (board-dead included), or a capture-only stub."""
    return (board_key(row) is None or bool(row.get("miss_reason"))
            or row.get("ats") == CAPTURE_ATS)


def upsert_company(conn: sqlite3.Connection, company: CompanyIn) -> int | None:
    """Insert or update a company by name. `company` is a dict of column->value.

    `tags` merge instead of overwrite: a company discovered by the local
    sourcing pass ("nc_local") and later by BCI discovery ("neural") keeps
    both scopes.

    A new row is stamped with `created_at`, and re-upserting the same name
    never moves that stamp -- it is the roster's birth record, not a
    last-touched field (`last_probed` is that one, and it does move):

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "Acme", "ats": "lever"})
    >>> born = conn.execute("SELECT created_at FROM companies").fetchone()[0]
    >>> _ = upsert_company(conn, {"name": "Acme", "ats": "greenhouse"})
    >>> conn.execute("SELECT created_at FROM companies").fetchone()[0] == born
    True

    Writing a board onto a row clears any miss recorded against it: a row
    that has an `ats` is a company, not a miss (see record_miss).

    >>> _ = record_miss(conn, "Zeta", "no-board-found")
    >>> _ = upsert_company(conn, {"name": "Zeta", "ats": "ashby", "active": 1})
    >>> conn.execute("SELECT miss_reason, miss_at FROM companies "
    ...              "WHERE name='Zeta'").fetchone()[:]
    (None, None)

    A different board for a name whose row has a LIVE one never replaces it:
    the board is added as a sibling (see add_board), and a miss recorded
    against the name loses its coordinates instead.

    >>> _ = upsert_company(conn, {"name": "Beta", "ats": "lever", "slug": "beta"})
    >>> _ = upsert_company(conn, {"name": "Beta", "ats": "ashby", "slug": "beta2"})
    >>> [r[0] for r in conn.execute("SELECT slug FROM companies WHERE name LIKE 'Beta%'")]
    ['beta', 'beta2']
    >>> _ = record_miss(conn, "Beta", "board-dead", ats="lever", slug="gone")
    >>> conn.execute("SELECT slug FROM companies WHERE name='Beta'").fetchone()[0]
    'beta'

    `watch` and `review` are facts of the employer, never scope tags. They are
    written as `watch` and `review` (a legacy `watch` / `pending-review` token
    in `tags` reads as the same), and no token is stored:

    >>> _ = upsert_company(conn, {"name": "Tok", "tags": "sweep,watch,pending-review"})
    >>> conn.execute("SELECT tags, review, watch FROM companies_effective "
    ...              "WHERE name='Tok'").fetchone()[:]
    ('sweep', 'pending', 1)
    >>> conn.execute("SELECT tags FROM companies WHERE name='Tok'").fetchone()[0]
    'sweep'
    """
    old = conn.execute("SELECT * FROM companies WHERE name=?", (company["name"],)).fetchone()
    if old and board_key(company) is not None:
        old = as_company(old)
        if not _board_gone(old) and board_key(old) != board_key(company):
            if not company.get("miss_reason"):
                return add_board(conn, company)[0]
            company = cast(CompanyIn, {k: v for k, v in company.items()
                                       if k not in _COORD_COLUMNS})
    return _write_company(conn, company, old=old)


def _write_company(conn: sqlite3.Connection, company: CompanyIn,
                   employer_id: int | None = None, *,
                   old: Mapping[str, object] | None | EllipsisType = ...) -> int | None:
    """upsert_company's write: insert or update by name, whatever the row
    already holds. `employer_id` places a NEW row under that employer (the
    insert trigger gives it one of its own otherwise).

    The mission columns, `review` and `watch` are the EMPLOYER's: they are
    written there (employers.py), and the board keeps the rest. A board's own
    verdict is set_board_mission's, never an upsert's. The two legacy tag
    tokens of the last two are converted here, once, and never stored. `old` is
    the row already named so (None: there is none); left out, it is looked up."""
    c: dict[str, object] = {**company, "last_probed": company.get("last_probed")
                         or datetime.now().isoformat()}
    c.setdefault("created_at", datetime.now().isoformat())
    # Drop None-valued keys: an upsert must never erase an existing value
    # (e.g. a failed/keyless mission-scoring pass writing mission_score=None
    # over a previously scored company). Inserts still get NULL defaults.
    c = {k: v for k, v in c.items() if v is not None}
    facts = {k: c.pop(k) for k in MISSION_COLS if k in c}
    review, watch = c.pop("review", None), c.pop("watch", None)
    raw_tags = c.get("tags")
    if raw_tags is not None and not isinstance(raw_tags, str):
        raise TypeError(f"company tags must be a string, got {type(raw_tags).__name__}")
    named = tags.parse(raw_tags)
    prior = (conn.execute("SELECT tags FROM companies WHERE name=?", (c["name"],)).fetchone()
             if isinstance(old, EllipsisType) else old)
    held = named | (tags.parse(cast("str | None", prior["tags"])) if prior else set())
    if scope := held - tags.FACT_TOKENS:
        c["tags"] = tags.join(scope)
    else:
        c.pop("tags", None)
    cols = [k for k in CompanyIn.__annotations__ if k in c]
    if employer_id is not None:
        c["employer_id"] = employer_id
        cols.append("employer_id")
    placeholders = ", ".join("?" for _ in cols)
    # created_at is written on INSERT but never overwritten on UPDATE: it is
    # the row's birth stamp, so a re-probe of a known company must leave it
    # (and a legacy NULL) alone.
    updates = ", ".join(f"{k}=excluded.{k}" for k in cols
                        if k not in ("name", "created_at", "employer_id"))
    conn.execute(
        f"INSERT INTO companies ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(name) DO UPDATE SET {updates}",
        [c[k] for k in cols],
    )
    if c.get("ats"):
        conn.execute("UPDATE companies SET miss_reason=NULL, miss_at=NULL "
                     "WHERE name=? AND miss_reason IS NOT NULL", (c["name"],))
    row = conn.execute("SELECT id FROM companies WHERE name=?", (c["name"],)).fetchone()
    if row:
        write_mission(conn, row["id"], **facts)
        apply_facts(conn, row["id"], review == "pending" or tags.PENDING in named,
                    bool(watch) or tags.WATCH in named)
    _commit(conn)
    return row["id"] if row else None


# --------------------------------------------------------------------------- #
#  Employers and boards                                                        #
# --------------------------------------------------------------------------- #
#
# An employer owns one or more boards; each `companies` row is ONE board and
# the crawl unit. Discovery asks add_board, never upsert_company, when it
# found a board for an employer the roster may already hold.

def _employer_id(conn: sqlite3.Connection, company: CompanyIn,
                 named: CompanyRow | None) -> int | None:
    """The employer a board belongs to, or None when the roster has none:
    the employer of the row already named so, an employer with the same
    name_key, or the employer of the roster row whose careers host `company`
    shares (company_by_host)."""
    if named and named.get("employer_id"):
        return named["employer_id"]
    key = _name_key(company["name"])
    found = _company_index(conn)["employer_ids"].get(key) if key else None
    if found:
        return found
    host_row = company_by_host(conn, company.get("careers_url"))
    return host_row["employer_id"] if host_row else None


def plan_board(conn: sqlite3.Connection, company: CompanyIn) -> BoardPlan:
    """Decide what add_board would do; writes nothing.

    The same board (board_key) already on the roster is an "update" of that
    row, whatever its name. Otherwise find the employer (_employer_id): one of
    its rows whose board is gone is "replaced", else the board is a "sibling";
    no known employer is "new". `needs_score` says whether it also takes a
    mission call.

    >>> conn = connect(":memory:")
    >>> _ = add_board(conn, {"name": "Acme", "ats": "lever", "slug": "acme"})
    >>> plan_board(conn, {"name": "Acme", "ats": "lever", "slug": "acme"}).action
    'update'
    >>> plan_board(conn, {"name": "Acme", "ats": "ashby", "slug": "acme"}).action
    'sibling'
    >>> plan_board(conn, {"name": "Other", "ats": "ashby", "slug": "o"}).action
    'new'
    >>> _ = record_miss(conn, "Lead", "no-board-found")
    >>> plan_board(conn, {"name": "Lead", "ats": "ashby", "slug": "lead"}).action
    'replace'

    >>> _ = upsert_company(conn, {"name": "Acme", "mission_tier": "core", "mission_score": 0.9})
    >>> [plan_board(conn, {"name": "Acme", "ats": "ashby", "slug": "acme"}).needs_score,
    ...  plan_board(conn, {"name": "Lead", "ats": "ashby", "slug": "lead"}).needs_score]
    [False, True]
    """
    named_row = conn.execute("SELECT * FROM companies_effective WHERE name=?",
                             (company["name"],)).fetchone()
    named = as_company(named_row) if named_row else None
    if board_key(company) is None:
        return BoardPlan("update" if named else "new", named,
                         named["employer_id"] if named else None)
    same = company_by_board(conn, company)
    if same:
        return BoardPlan("update", same, same["employer_id"])
    emp = _employer_id(conn, company, named)
    if emp is None:
        return BoardPlan("new")
    rows = [as_company(r) for r in conn.execute(
        "SELECT * FROM companies_effective WHERE employer_id=? ORDER BY id", (emp,))]
    if not rows:
        return BoardPlan("new", None, emp)
    rows.sort(key=lambda r: r["name"] != company["name"])
    held = conn.execute("SELECT mission_tier, mission_score, mission_reason FROM employers "
                        "WHERE id=?", (emp,)).fetchone()
    verdict = (held["mission_tier"], held["mission_score"], held["mission_reason"] or "") \
        if held and held["mission_score"] is not None else None
    gone = next((r for r in rows if _board_gone(r)), None)
    if gone:
        return BoardPlan("replace", gone, emp, verdict)
    primary = min(rows, key=lambda r: (r["mission_tier"] is None, r["id"]))
    return BoardPlan("sibling", primary, emp, verdict)


def add_board(conn: sqlite3.Connection, company: CompanyIn) -> tuple[int | None, str]:
    """Write a board the roster may already hold, never losing a working one.
    Returns (company id, the BoardPlan action); see plan_board.

    A sibling is a new row "<employer> (<ats>)" under the same employer. It
    INHERITS the employer's mission verdict, review state and watch (nothing
    is copied, so no mission call, and a verdict in `company` is dropped when
    the employer has one; set_board_mission scores a board on its own). Its
    scope tags are its own; its `active` is `company`'s, else follows the
    employer's live boards (the mission rule's answer when it has none).
    The caller has validated the board live. A "replace" clears the old
    coordinates first.

    >>> conn = connect(":memory:")
    >>> a, _ = add_board(conn, {"name": "Acme", "ats": "lever", "slug": "acme",
    ...                         "mission_tier": "core", "mission_score": 0.9, "tags": "local"})
    >>> b, how = add_board(conn, {"name": "Acme", "ats": "workday", "handle": "acme|5|ext"})
    >>> how, conn.execute("SELECT name, mission_score, tags FROM companies_effective WHERE id=?",
    ...                   (b,)).fetchone()[:]
    ('sibling', ('Acme (workday)', 0.9, None))
    >>> conn.execute("SELECT mission_score FROM companies WHERE id=?", (b,)).fetchone()[0] is None
    True
    >>> conn.execute("SELECT COUNT(DISTINCT employer_id), COUNT(*) FROM companies").fetchone()[:]
    (1, 2)

    """
    plan = plan_board(conn, company)
    row = plan.row
    if plan.action == "sibling":
        return _write_sibling(conn, company, cast(int, plan.employer_id)), plan.action
    if plan.action == "new":
        return _write_company(conn, company, plan.employer_id), plan.action
    row = cast(CompanyRow, row)
    if plan.action == "replace":
        conn.execute("UPDATE companies SET slug=NULL, handle=NULL WHERE id=?", (row["id"],))
    return _write_company(conn, {**company, "name": row["name"]}), plan.action


def _write_sibling(conn: sqlite3.Connection, company: CompanyIn, employer_id: int) -> int | None:
    """add_board's new row for a board that is another of an employer's."""
    base, tier, has_verdict = conn.execute(
        "SELECT name, mission_tier, mission_tier IS NOT NULL OR mission_score IS NOT NULL "
        "FROM employers WHERE id=?", (employer_id,)).fetchone()
    label, n = company.get("ats") or "board", 1
    name = f"{base} ({label})"
    while conn.execute("SELECT 1 FROM companies WHERE name=?", (name,)).fetchone():
        n += 1
        name = f"{base} ({label} {n})"
    row: dict[str, object] = {k: v for k, v in company.items() if v is not None}
    if has_verdict:
        for k in MISSION_COLS:
            row.pop(k, None)
    row["name"] = name
    live = conn.execute("SELECT MAX(active) FROM companies WHERE employer_id=? "
                        "AND miss_reason IS NULL", (employer_id,)).fetchone()[0]
    row.setdefault("active", config.is_active_mission(tier, base) if live is None else live)
    return _write_company(conn, cast(CompanyIn, row), employer_id)


# --------------------------------------------------------------------------- #
#  Misses                                                                      #
# --------------------------------------------------------------------------- #
#
# A candidate that fails to become a crawlable company used to be printed and
# thrown away, so the same name failed the same way on every run with no
# record of why. It is now kept as an INACTIVE companies row carrying a
# machine-readable `miss_reason` and a `miss_at` retry stamp. Same table on
# purpose: name/source/careers_url/ats are exactly the columns a miss needs
# to record, resolve_leads() already reprocesses boardless inactive rows, and
# `active = 0` is the crawl's existing "do not fetch" switch -- a parallel
# table would duplicate all three and add a second place a name can hide.

# Reason FAMILIES. A stored reason is a family, optionally ':'-qualified with
# the offending platform or error ("ats-unsupported:ukg",
# "fetch-error:ReadTimeout"); miss_counts aggregates on the family so the
# qualifier stays readable without fragmenting the tally.
MISS_REASONS = (
    # no-board-found qualifiers (src.discovery.resolve.sniffer.diagnose_no_board):
    #   :wrong-domain          a candidate resolved to an unrelated company
    #   :domain-unreachable    not one candidate URL answered
    #   :careers-page-no-ats   real job board found, but no known ATS on it
    #   :site-only-no-careers  domain answers, nothing careers-shaped on it
    "no-board-found",   # nothing resolved: sniff, slug-probe and websearch all missed
    "board-dead",       # coordinates detected, but the live fetch returns nothing
    "ats-unsupported",  # a real ATS we recognize but cannot fetch (:platform)
    "no-local-jobs",    # board live and readable, zero openings in [locality]
    "fetch-error",      # the resolution attempt itself raised (:ExceptionName)
)


def miss_family(reason: str | None) -> str:
    """The family part of a miss reason: the token before any ':' qualifier.

    >>> miss_family("no-local-jobs")
    'no-local-jobs'
    >>> miss_family("ats-unsupported:ukg")
    'ats-unsupported'
    >>> miss_family(None)
    ''
    """
    return (reason or "").split(":", 1)[0]


def record_miss(conn: sqlite3.Connection, name: str, reason: str, /,
                **fields: Unpack[CompanyIn]) -> bool:
    """Record that `name` failed to become a crawlable company, and why.

    The row is always written inactive, so it is invisible to every crawl
    path (all of which read get_companies(active_only=True)):

    >>> conn = connect(":memory:")
    >>> _ = record_miss(conn, "Chiesi USA", "no-local-jobs", ats="greenhouse")
    >>> [c["name"] for c in get_companies(conn, active_only=True)]
    []
    >>> [(c["name"], c["miss_reason"], c["active"])
    ...  for c in get_companies(conn, active_only=False)]
    [('Chiesi USA', 'no-local-jobs', 0)]

    Re-recording the same name updates the reason in place rather than
    growing a second row, so a name that keeps failing stays one worklist
    entry:

    >>> _ = record_miss(conn, "Chiesi USA", "board-dead")
    >>> [(c["name"], c["miss_reason"])
    ...  for c in get_companies(conn, active_only=False)]
    [('Chiesi USA', 'board-dead')]

    An ACTIVE company is never demoted by a miss -- a transient failure while
    re-probing a working board must not drop it out of the roster. Returns
    True when a miss was written, False when it was declined:

    >>> _ = upsert_company(conn, {"name": "Locus", "ats": "lever", "active": 1})
    >>> record_miss(conn, "Locus", "fetch-error:ReadTimeout")
    False
    >>> [c["name"] for c in get_companies(conn, active_only=True)]
    ['Locus']

    A transient failure (`fetch-error:*`) is no verdict on the board: it
    keeps a recorded dead or missing board's reason, restamping only when:

    >>> record_miss(conn, "Chiesi USA", "fetch-error:stalled")
    True
    >>> [c["miss_reason"] for c in get_companies(conn, active_only=False)
    ...  if c["name"] == "Chiesi USA"]
    ['board-dead']

    Notes:
        The 2026-10-07 reresolve stalled on every name and stamped 50
        board-dead rows `fetch-error:stalled`, which the harvester reads
        as a live board: it pulled their dead slugs again that night.
    """
    row = conn.execute("SELECT active, miss_reason FROM companies_effective WHERE name=?",
                       (name,)).fetchone()
    if row and row["active"]:
        return False
    now = datetime.now().isoformat()
    if (row and reason.startswith("fetch-error")
            and (row["miss_reason"] or "").startswith(("board-dead", "no-board-found"))):
        conn.execute("UPDATE companies SET miss_at=? WHERE name=?", (now, name))
        _commit(conn)
        return True
    upsert_company(conn, {**fields, "name": name, "active": 0,
                          "miss_reason": reason, "miss_at": now})
    # upsert_company clears the miss columns whenever an `ats` is written (a
    # row with a board is a company) -- but here the ats is part of the miss
    # record itself ("board-dead" knows which board died), so put them back.
    conn.execute("UPDATE companies SET miss_reason=?, miss_at=? WHERE name=?",
                 (reason, now, name))
    _commit(conn)
    return True


def miss_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Misses per reason family, biggest first: the "where are we losing
    companies" tally.

    >>> conn = connect(":memory:")
    >>> for n, r in [("a", "no-local-jobs"), ("b", "no-local-jobs"),
    ...              ("c", "ats-unsupported:ukg"),
    ...              ("d", "ats-unsupported:taleo")]:
    ...     _ = record_miss(conn, n, r)
    >>> miss_counts(conn)
    [('ats-unsupported', 2), ('no-local-jobs', 2)]
    """
    rows = conn.execute("SELECT miss_reason FROM companies "
                        "WHERE miss_reason IS NOT NULL").fetchall()
    tally: dict[str, int] = {}
    for r in rows:
        fam = miss_family(r["miss_reason"])
        tally[fam] = tally.get(fam, 0) + 1
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))


def recent_miss_names(conn: sqlite3.Connection, days: int = 14) -> set[str]:
    """Names whose miss was recorded within `days`: the set a rerun skips
    instead of re-probing.

    >>> conn = connect(":memory:")
    >>> _ = record_miss(conn, "Fresh", "no-board-found")
    >>> _ = record_miss(conn, "Stale", "no-board-found")
    >>> _ = conn.execute("UPDATE companies SET miss_at=? WHERE name='Stale'",
    ...                  ((datetime.now() - timedelta(days=99)).isoformat(),))
    >>> sorted(recent_miss_names(conn, days=14))
    ['Fresh']

    days=0 disables the skip, so a retry-everything run re-probes the lot:

    >>> recent_miss_names(conn, days=0)
    set()
    """
    if not days:
        return set()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    return {r["name"] for r in conn.execute(
        "SELECT name FROM companies WHERE miss_reason IS NOT NULL "
        "AND miss_at IS NOT NULL AND miss_at >= ?", (cutoff,)).fetchall()}


def roster_growth(conn: sqlite3.Connection, days: int = 7) -> int:
    """How many companies joined the roster in the last `days`.

    Counts `created_at`, not `last_probed`: bulk mission re-scoring rewrites
    last_probed on every row, so only created_at can answer "did the roster
    grow this week".

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "New Co", "ats": "lever"})
    >>> roster_growth(conn, days=7)
    1

    Rows that predate the column (created_at NULL on an upgraded DB) are
    never counted as growth:

    >>> _ = conn.execute("INSERT INTO companies (name) VALUES ('Legacy Co')")
    >>> roster_growth(conn, days=7)
    1

    Neither are misses. A pass that resolves nothing but files fifty
    failures grew the WORKLIST, not the roster, and must not read as growth:

    >>> _ = record_miss(conn, "Nope Bio", "no-board-found")
    >>> roster_growth(conn, days=7)
    1
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    return cast(int, conn.execute(
        "SELECT COUNT(*) FROM companies "
        "WHERE created_at >= ? AND miss_reason IS NULL",
        (cutoff,)).fetchone()[0])


# --------------------------------------------------------------------------- #
#  Company identity, dedup, and roster CRUD (incl. capture-only rows)          #
# --------------------------------------------------------------------------- #
#
# Some of the best employers cannot be fetched at all: the careers host
# answers a plain request with a bot challenge, or the board is rendered by
# JavaScript on a site with no ATS signature, so discovery left them inactive
# as "no-board-found". For those the person drives the browser (capture.py)
# and the crawler parses only what they saved. Such a company carries
# ``ats = CAPTURE_ATS``: a real roster row, but one no crawl path may fetch.
# crawlable_companies leaves it out, so it never earns an empty streak or a
# fetch error for a board nobody asked.
CAPTURE_ATS = "capture"


def _split_url(url: str | None) -> tuple[str, str]:
    """(host, path) of an http(s) URL, host lower-cased without a leading
    ``www.``; ('', '') for anything else.

    >>> _split_url("https://WWW.Acme.org/careers/jobs?x=1")
    ('acme.org', '/careers/jobs')
    >>> _split_url("jobs.acme.org")
    ('', '')
    """
    m = re.match(r"https?://([^/?#]+)([^?#]*)", (url or "").strip(), re.I)
    if not m:
        return "", ""
    host = re.sub(r"^www\.", "", m.group(1).lower())
    return host, (m.group(2) or "/")


def _board_prefix(path: str) -> str:
    """The first path segment of a careers URL, the piece that names a tenant
    on a shared host ('/axoft/40863' -> '/axoft'); '' for a bare origin."""
    seg = path.strip("/").split("/")[0] if path else ""
    return f"/{seg}" if seg else ""


def company_by_host(conn: sqlite3.Connection, url: str | None) -> CompanyRow | None:
    """The roster company whose careers_url (or URL-shaped slug) claims the
    host of `url`, or None. The capture path asks this so a page saved from an
    employer's own careers site lands under that employer's EXISTING row
    instead of minting a new company.

    An exact host match wins; a sibling host on the same company-owned domain
    is accepted too (careers sites live on jobs./careers. subdomains, the
    roster usually holds www.):

    >>> conn = connect(":memory:")
    >>> _ = record_miss(conn, "Acme Health", "no-board-found",
    ...                 careers_url="https://www.acmehealth.org/careers/")
    >>> company_by_host(conn, "https://www.acmehealth.org/careers/jobs")["name"]
    'Acme Health'
    >>> company_by_host(conn, "https://jobs.acmehealth.org/search/jobs")["name"]
    'Acme Health'
    >>> company_by_host(conn, "https://jobs.otherhealth.org/") is None
    True

    On a multi-tenant host the domain proves nothing, so the page must sit
    under the board's own first path segment:

    >>> _ = upsert_company(conn, {"name": "Beta Labs",
    ...                           "careers_url": "https://jobs.polymer.co/beta"})
    >>> company_by_host(conn, "https://jobs.polymer.co/beta/40863")["name"]
    'Beta Labs'
    >>> company_by_host(conn, "https://jobs.polymer.co/gamma") is None
    True

    Anything that is not an http(s) URL matches nothing:

    >>> company_by_host(conn, "") is None
    True
    """
    host, path = _split_url(url)
    if not host:
        return None
    # config.SHARED_HOSTS are multi-tenant: a domain match proves nothing
    # there, so the board's own path must match.
    shared = bool(config.hosts_re(config.SHARED_HOSTS).search(host))
    idx = _company_index(conn)
    for c, cpath in idx["by_host"].get(host, ()):
        prefix = _board_prefix(cpath) if shared else ""
        if not prefix or path.lower().startswith(prefix.lower()):
            return cast(CompanyRow, dict(c))
    if shared:
        return None
    for c, chost in idx["by_domain"].get(_domain(host), ()):
        if chost != host:
            return cast(CompanyRow, dict(c))
    return None


def _board_columns(ats: str) -> tuple[HandleColumn, ...]:
    """The columns naming an ATS's board: config.BOARDS `handle.columns`,
    default the slug; a capture-only board is named by its careers_url."""
    if ats == CAPTURE_ATS:
        return ("careers_url",)
    columns = dig(config.BOARDS, ats, "handle", "columns")
    return config.DEFAULT_HANDLE_COLUMNS if columns is None else cast(tuple[HandleColumn, ...], columns)


type BoardKey = tuple[str, *tuple[object, ...]]


class _CompanyIndex(TypedDict):
    """What _build_company_index returns, key by key."""
    rows: list[CompanyRow]
    by_board: defaultdict[BoardKey, list[CompanyRow]]
    by_host: defaultdict[str, list[tuple[CompanyRow, str]]]
    by_domain: defaultdict[str, list[tuple[CompanyRow, str]]]
    employer_ids: dict[str, int]


class _Duplicate(TypedDict):
    """One dedup_companies window row: `shared` columns plus the ranking."""
    id: int
    name: str
    tags: str | None
    active: int | None
    mission_tier: str | None
    review: str | None
    watch: int | None
    board: str
    g: int
    rn: int
    any_active: int


def board_key(r: BoardCoords) -> BoardKey | None:
    """The identity of a company row's BOARD, independent of its name:
    (ats, *the values of the columns its spec's handle names), a
    careers_url lowercased with no trailing "/". None when the row has no
    ats or its first board column is empty. Shared by dedup_companies
    (merging after the fact) and company_by_board (refusing the duplicate
    before it lands).

    careers_url-keyed ATSes: their slug is a shared datacenter host
    (SuccessFactors "performancemanagerN" serves many tenants) or absent,
    and the careers_url IS the board identity. Keying these on slug merged
    Bayer into Sonova (both performancemanager5).

    >>> board_key({"ats": "workday", "handle": "redhat|5|jobs"})
    ('workday', 'redhat|5|jobs')
    >>> board_key({"ats": "icims", "slug": "globalcareers-sas", "handle": None})
    ('icims', 'globalcareers-sas')
    >>> board_key({"ats": "custom", "slug": None, "handle": None,
    ...            "careers_url": "https://x.com/careers/"})
    ('custom', 'https://x.com/careers')
    >>> board_key({"ats": None, "slug": None, "handle": None}) is None
    True

    A site's case is not identity: Workday answers `External` and `external`
    alike, and the roster held 7 boards twice that way (2026-10-06).

    >>> a = {"ats": "workday", "handle": "aah|5|External"}
    >>> board_key(a) == board_key({**a, "handle": "AAH|5|external"})
    True

    Only a host that answers case alike (handle `fold`) is folded; another's
    slugs are case-sensitive:

    >>> board_key({"ats": "lever", "slug": "Acme"}) == board_key({"ats": "lever", "slug": "acme"})
    False
    """
    ats = r.get("ats")
    if not ats:
        return None
    cols = _board_columns(ats)
    fold = bool(dig(config.BOARDS, ats, "handle", "fold"))
    vals = [(v.rstrip("/").lower() if fold or c == "careers_url" else v.rstrip("/"))
            if isinstance(v := r.get(c), str) else v for c in cols]
    return (ats, *vals) if vals[0] else None


def _domain(host: str) -> str:
    """The registrable-ish tail of a host, the piece two sibling careers
    hosts share ('jobs.acme.org' -> 'acme.org')."""
    return ".".join(host.split(".")[-2:])


#: This run's company index per connection: conn -> (store stamp, index).
_Memo = dict[sqlite3.Connection, tuple[tuple[int, int], _CompanyIndex]]
_INDEXES: Callable[[], _Memo] = per_run(dict)


def _company_index(conn: sqlite3.Connection) -> _CompanyIndex:
    """The roster scanned once for the identity lookups (company_by_host,
    _employer_id, dedup_companies), held per connection for the run. A write
    through this connection (`total_changes`) or another (`PRAGMA
    data_version`) invalidates it, and with uncommitted writes it is rebuilt
    each call, so a caller always sees its own writes. Outside a run nothing
    holds it. Callers must not mutate what it returns.

    Returns a dict:

    * ``rows``       every row as a dict, id order
    * ``by_board``   board_key -> rows with that board, id order
    * ``by_host``    careers host -> [(row, path)], id order, a row's
                     careers_url candidate before its URL-shaped slug
    * ``by_domain``  _domain(host) -> [(row, host)], same order
    * ``employer_ids``  name_key -> the lowest employer id with that key

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "A", "ats": "lever", "slug": "a",
    ...                           "careers_url": "https://www.a.org/jobs/"})
    >>> _ = upsert_company(conn, {"name": "B", "ats": "lever", "slug": "a"})
    >>> idx = _company_index(conn)
    >>> [r["name"] for r in idx["rows"]]
    ['A', 'B']
    >>> [r["name"] for r in idx["by_board"][("lever", "a")]]
    ['A', 'B']
    >>> [(r["name"], p) for r, p in idx["by_host"]["a.org"]]
    [('A', '/jobs/')]
    >>> [(r["name"], h) for r, h in idx["by_domain"]["a.org"]]
    [('A', 'a.org')]
    >>> idx["employer_ids"]
    {'a': 1, 'b': 2}
    """
    stamp = (conn.total_changes, conn.execute("PRAGMA data_version").fetchone()[0])
    memo: _Memo
    try:
        memo = _INDEXES()
    except RuntimeError:                  # no run (a doctest, a script): nothing holds it
        memo = {}
    held = memo.get(conn)
    if held and held[0] == stamp:
        return held[1]
    idx = _build_company_index(conn)
    if not conn.in_transaction:
        memo[conn] = (stamp, idx)
    return idx


def _build_company_index(conn: sqlite3.Connection) -> _CompanyIndex:
    """One scan of the roster for _company_index."""
    rows = [as_company(r) for r in
            conn.execute("SELECT * FROM companies_effective ORDER BY id").fetchall()]
    by_board: defaultdict[BoardKey, list[CompanyRow]] = defaultdict(list)
    by_host: defaultdict[str, list[tuple[CompanyRow, str]]] = defaultdict(list)
    by_domain: defaultdict[str, list[tuple[CompanyRow, str]]] = defaultdict(list)
    for c in rows:
        key = board_key(c)
        if key is not None:
            by_board[key].append(c)
        for cand in (c.get("careers_url"), c.get("slug")):
            if not cand or "." not in cand:
                continue
            if not re.match(r"https?://", cand, re.I):
                cand = f"https://{cand}"
            chost, cpath = _split_url(cand)
            if not chost:
                continue
            by_host[chost].append((c, cpath))
            by_domain[_domain(chost)].append((c, chost))
    employer_ids: dict[str, int] = {}
    for e in conn.execute("SELECT id, name FROM employers ORDER BY id"):
        employer_ids.setdefault(_name_key(e["name"]), e["id"])
    return {"rows": rows, "by_board": by_board, "by_host": by_host,
            "by_domain": by_domain, "employer_ids": employer_ids}


def company_by_board(conn: sqlite3.Connection, row: BoardCoords) -> CompanyRow | None:
    """The existing company row whose board matches `row`'s (see board_key),
    or None. Discovery asks it before inserting, because a name the roster
    spells differently passes the name-keyed already-tracked check and
    would land as a second row on the same board.

    Notes:
        "SAS" vs "SAS Institute", "Veeva Systems" vs "Veeva" and "NVIDIA AI"
        vs "NVIDIA" were all re-added on 2026-09-01; until the next dedup
        the crawl fetched each board twice and the ranking showed two
        companies.
    """
    key = board_key(row)
    if key is None:
        return None
    # SQL narrows to the rows sharing the ats and every plain handle column,
    # case aside (careers_url is normalised by board_key, so it is left to the check);
    # board_key then confirms, so the identity rule stays in one place.
    plain = [(c, v) for c, v in zip(_board_columns(key[0]), key[1:]) if c != "careers_url"]
    where = " AND ".join(["ats = ?", *(f"{c} IS ? COLLATE NOCASE" for c, _ in plain)])
    rows = conn.execute(f"SELECT * FROM companies_effective WHERE {where} ORDER BY id",
                        [key[0], *(v for _, v in plain)])
    return next((c for c in map(as_company, rows) if board_key(c) == key), None)


def dedup_companies(conn: sqlite3.Connection) -> int:
    """Merge company rows that point at the SAME board (same ats+slug, or the
    same Workday triple) under different name spellings ("IQVIA" vs "Quintiles
    IMS (IQVIA)"), which the name-keyed upsert can't catch. Jobs are
    re-pointed to the kept row and tags merge. Returns rows merged.

    Rows that are different boards of one employer (add_board's siblings)
    are never merged, and a merged row's employer takes over its losers':

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> _ = add_board(conn, {"name": "Acme", "ats": "ashby", "slug": "a"})
    >>> _ = upsert_company(conn, {"name": "Acme Inc", "ats": "lever", "slug": "a"})
    >>> dedup_companies(conn)
        Acme                           <- merged 1: Acme Inc
    1
    >>> [r[:] for r in conn.execute("SELECT c.name, e.name FROM companies c "
    ...                             "JOIN employers e ON e.id = c.employer_id")]
    [('Acme', 'Acme'), ('Acme (ashby)', 'Acme')]
    """
    # board_key is Python; the survivor is SQL: a scored row, then active,
    # then most-referenced, then the shortest name, then the oldest.
    shared = [[c["id"], json.dumps(key)]
              for key, rows in _company_index(conn)["by_board"].items() if len(rows) > 1
              for c in rows]
    groups: dict[int, list[_Duplicate]] = {}
    for r in conn.execute("""
            WITH shared AS (
              SELECT c.id, c.name, c.tags, c.active, c.mission_tier, c.review, c.watch, k.board
              FROM (SELECT json_extract(value, '$[0]') AS id,
                           json_extract(value, '$[1]') AS board FROM json_each(?)) k
              JOIN companies_effective c ON c.id = k.id
            )
            SELECT s.*, MIN(s.id) OVER same AS g,
                   ROW_NUMBER() OVER (same ORDER BY s.mission_tier IS NULL,
                                      -COALESCE(s.active, 0), -COALESCE(n.jobs, 0),
                                      length(COALESCE(s.name, '')), s.id) AS rn,
                   MAX(COALESCE(s.active, 0)) OVER same != 0 AS any_active
            FROM shared s
            LEFT JOIN (SELECT company_id, COUNT(*) AS jobs FROM jobs GROUP BY company_id) n
                   ON n.company_id = s.id
            WINDOW same AS (PARTITION BY s.board)
            ORDER BY g, rn""", (json.dumps(shared),)):
        groups.setdefault(r["g"], []).append(cast(_Duplicate, dict(r)))

    def carry_over(keep: _Duplicate, losers: list[_Duplicate]) -> None:
        merged = {t for m in [keep, *losers] for t in tags.parse(m.get("tags"))}
        # Rename as well as re-point: jobs.company_name is the grouping key
        # (ranked_jobs, the digest).
        lost = json.dumps([l["id"] for l in losers])
        conn.execute("UPDATE jobs SET company_id=?, company_name=? WHERE company_id IN "
                     "(SELECT value FROM json_each(?))", (keep["id"], keep["name"], lost))
        # The losers' employers (and their other boards) join the survivor's.
        conn.execute("UPDATE companies SET employer_id=(SELECT employer_id FROM companies "
                     "WHERE id=?) WHERE employer_id IN (SELECT employer_id FROM companies "
                     "WHERE id IN (SELECT value FROM json_each(?)))", (keep["id"], lost))
        apply_update(conn, "companies", "id", keep["id"],
                     {"tags": tags.join(merged), "active": keep["any_active"]})
        everyone = [keep, *losers]
        apply_facts(conn, keep["id"], any(m["review"] == "pending" for m in everyone),
                    any(m["watch"] for m in everyone))

    merged = dedup_groups(
        conn, "companies", "id", groups, rank=lambda r: r["rn"], merge=carry_over,
        describe=lambda keep, losers: (
            f"{keep['name'][:30]:30} <- merged {len(losers)}: "
            + ", ".join(l["name"][:20] for l in losers)))
    realign_job_names(conn)
    conn.execute("DELETE FROM employers WHERE NOT EXISTS "
                 "(SELECT 1 FROM companies WHERE employer_id = employers.id)")
    _commit(conn)
    return merged


def realign_job_names(conn: sqlite3.Connection) -> int:
    """Give every linked job its EMPLOYER's current name (its board's own
    when it has no employer), in one UPDATE; returns the rows renamed. This
    catches rows renamed by earlier (name-blind) merges and rows whose
    ingest path spelled the company its own way ("BD (Becton Dickinson)"
    linked to company "BD"), and shows a sibling board's jobs under its
    employer.

    >>> conn = connect(":memory:")
    >>> a = upsert_company(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> b, _ = add_board(conn, {"name": "Acme", "ats": "ashby", "slug": "b"})
    >>> _ = conn.executemany("INSERT INTO jobs (job_id, company_id, company_name) "
    ...                      "VALUES (?, ?, 'x')", [("j1", a), ("j2", b)])
    >>> realign_job_names(conn)
        2 job row(s) renamed to their employer's name
    2
    >>> sorted(r[0] for r in conn.execute("SELECT DISTINCT company_name FROM jobs"))
    ['Acme']
    """
    renamed = conn.execute(
        "UPDATE jobs SET company_name=(SELECT employer_name FROM board_employers b "
        "WHERE b.company_id=jobs.company_id) WHERE company_id IN "
        "(SELECT id FROM companies) AND company_name IS NOT "
        "(SELECT employer_name FROM board_employers b WHERE b.company_id=jobs.company_id)"
    ).rowcount
    if renamed:
        print(f"    {renamed} job row(s) renamed to their employer's name")
    return renamed


def export_companies(conn: sqlite3.Connection, path: str | Path) -> int:
    """Dump the company roster to JSON — the shareable/bootstrap artifact
    that replaced config.py's seed lists. Secrets-free by construction. A
    row carries its board's EFFECTIVE facts, flat: employer links and board
    overrides are not exported."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM companies_effective ORDER BY name").fetchall()]
    for r in rows:
        r.pop("id", None)          # ids are per-database
        r.pop("employer_id", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    return len(rows)


def _unwritten(row: object) -> object:
    """The row without the columns an upsert never writes (id, the crawl
    schedule); anything else it holds is left for validation to judge."""
    skip = CompanyRow.__annotations__.keys() - CompanyIn.__annotations__.keys()
    return {k: v for k, v in row.items() if k not in skip} if isinstance(row, dict) else row


def _checked(row: CompanyIn) -> CompanyIn:
    """The row, or a ValueError when it names no company or a review state."""
    if "name" not in row:
        raise ValueError("name is required")
    if row.get("review") not in (None, "pending", "confirmed"):
        raise ValueError("review is 'pending' or 'confirmed'")
    return row


_IMPORT_ROWS = TypeAdapter(list[Annotated[CompanyIn, BeforeValidator(_unwritten), AfterValidator(_checked)]],
                           config=ConfigDict(extra="forbid"))


def import_companies(conn: sqlite3.Connection, path: str | Path) -> int:
    """Upsert companies from an export_companies JSON file (idempotent;
    tags merge, existing mission scores survive None fields).

    Every row must be an object with a non-empty `name`, only columns the
    companies table has, and a value each column's type accepts (`"5"` and
    `5.0` load as 5; `5.5` does not); otherwise pydantic.ValidationError
    names each bad row path and nothing is written. Columns an upsert never
    writes (id, the crawl schedule) are accepted and ignored. Enforced by
    tests/test_store.py::TestImportCompanies.
    """
    with open(path, "rb") as f:
        rows = _IMPORT_ROWS.validate_json(f.read())
    for row in rows:
        upsert_company(conn, row)
    return len(rows)


def set_company_tag(conn: sqlite3.Connection, name: str, tag: str,
                    add: bool = True) -> str | None:
    """Add or remove one scope tag on a company (case-insensitive name
    match). Returns its new comma-joined tags ('' when none), or None if no
    such company exists. Watching is set_watch's, not a tag.

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> set_company_tag(conn, "acme", "sweep")
    'sweep'
    >>> set_company_tag(conn, "ACME", "nc_local")
    'local,sweep'
    >>> set_company_tag(conn, "acme", "sweep", add=False)
    'local'
    >>> set_company_tag(conn, "nope", "sweep") is None
    True
    """
    row = conn.execute("SELECT id, tags FROM companies WHERE lower(name)=lower(?)",
                       (name,)).fetchone()
    if not row:
        return None
    held = tags.parse(row["tags"])
    (held.add if add else held.discard)(tags.canonical(tag))
    new = tags.join(held)
    conn.execute("UPDATE companies SET tags=? WHERE id=?", (new, row["id"]))
    _commit(conn)
    return new or ""


# NEAR-MISS, DELIBERATE: different queries (row-by-id vs column-by-name);
# merging needs a query builder, not a lookup.

def get_company(conn: sqlite3.Connection, company_id: int | None) -> CompanyRow | None:
    """One company row by id, or None."""
    if not company_id:
        return None
    row = conn.execute("SELECT * FROM companies_effective WHERE id=?", (company_id,)).fetchone()
    return as_company(row) if row else None


def company_id_by_name(conn: sqlite3.Connection, name: str | None) -> int | None:
    """Resolve a company name to its id (case-insensitive exact match), or
    None if the store has no such company. Used to link externally-ingested
    jobs to their vetted company row so they inherit its mission score."""
    if not name:
        return None
    row = conn.execute(
        "SELECT id FROM companies WHERE lower(name) = lower(?) LIMIT 1",
        (name,)).fetchone()
    return row["id"] if row else None


def get_companies(conn: sqlite3.Connection, active_only: bool = True,
                  missions: Collection[str] | None = None,
                  tag: str | None = None, due_at: str | None = None) -> list[CompanyRow]:
    """Companies (their effective facts), optionally filtered by mission
    tier(s), scope `tag`, and `due_at` (an ISO time): only rows with a
    board due for a crawl then (crawlable_companies)."""
    q = "SELECT * FROM companies_effective"
    conds: list[str] = []
    args: list[str] = []
    if active_only:
        conds.append("active = 1")
    if due_at is not None:
        # NULL or '' crawl_state reads as 'active'; a dormant row is due once
        # next_crawl_at has passed; 'off' never. A capture row has no board.
        conds.append("ats IS NOT ? AND CASE COALESCE(NULLIF(crawl_state, ''), 'active') "
                     "WHEN 'active' THEN 1 WHEN 'dormant' THEN "
                     "COALESCE(next_crawl_at, '') <= ? ELSE 0 END")
        args += [CAPTURE_ATS, due_at]
    if missions:
        conds.append(f"mission_tier IN ({','.join('?' for _ in missions)})")
        args += list(missions)
    if tag:
        # tags is a comma-joined token list; match the token exactly.
        conds.append("(',' || COALESCE(tags,'') || ',') LIKE ?")
        args.append(f"%,{tag},%")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY mission_score DESC, local_job_count DESC"
    return [as_company(r) for r in conn.execute(q, args).fetchall()]


# --------------------------------------------------------------------------- #
#  Crawl scheduling (dormancy)                                                 #
# --------------------------------------------------------------------------- #
#
# A crawl of the local roster spent most of its wall clock on boards that
# never pay: 181 of 300 active companies had produced zero jobs ever, and
# three high-volume boards produced hundreds of rows nobody would apply to
# (a state health agency: 663 local jobs, best fit 0.15; a games studio;
# a health startup: 30 jobs, best fit 0.03). Deactivating them by hand is
# wrong -- a silent board can start hiring -- so they go DORMANT instead:
# still crawled, just weekly rather than every run.
#
# Two ways in, both reversible by the board itself:
#   * empty streak -- `dormant_after` consecutive DAYS returning nothing,
#     from a board with no open postings on file either (a location-scoped
#     read of a big employer with nothing local is not an empty board);
#   * off-mission volume -- >= 30 jobs stored and a best fit under 0.20.
# Watched companies are exempt from both: watching means "tell me the
# moment anything opens here", which a weekly cadence would break.

# Off-mission volume rule. A board this big that has never scored above
# this is not a scoring accident, it is the wrong employer for the profile.
_OFFMISSION_MIN_JOBS = 30


def _offmission_volume(conn: sqlite3.Connection, company_id: int) -> bool:
    """True when this company has stored >= 30 jobs and its BEST resume fit
    is still under 0.20 -- the high-volume off-mission board pattern. A NULL
    max (nothing scored yet) is missing data, not a verdict, so it fails."""
    # Scored rows only: the harvester stores every posting on a board
    # unscored, and 500 unjudged rows next to five scored ones say nothing
    # about the employer.
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(resume_fit_score) AS best FROM jobs "
        "WHERE company_id = ? AND resume_fit_score IS NOT NULL",
        (company_id,)).fetchone()
    return bool(row and row["n"] >= _OFFMISSION_MIN_JOBS
                and row["best"] is not None
                and row["best"] < 0.20)


def record_crawl_outcome(conn: sqlite3.Connection, company_id: int, n_jobs: int,
                         err: BaseException | None = None, dormant_after: int = 4,
                         dormant_days: int = 7) -> str | None:
    """Stamp one company's crawl result and re-decide its crawl_state.

    `n_jobs` is what the board returned for this track (already location
    filtered), `err` the fetch exception if any. Returns the row's new
    crawl_state.

    Rules, in order:
      * a fetch ERROR is neutral -- a 503 or a timeout is our problem, not
        evidence the board is dead, and counting it would retire companies
        during a network wobble;
      * `n_jobs == 0` grows empty_streak, but only ONCE PER CALENDAR DAY:
        several tracks (and a re-run after a crash) hit the same board on
        the same day, and three runs in one afternoon must not read as
        three empty days;
      * `n_jobs > 0` resets the streak and wakes a dormant row, and so do
        open postings on file (the harvester's whole-board read): a board
        serving jobs elsewhere is alive, whatever this track's scope kept;
      * either dormancy rule (streak, off-mission volume) parks the row at
        now + `dormant_days`.

    Watched companies, and rows the user switched 'off', are left alone.
    """
    row = conn.execute(
        "SELECT id, watch, crawl_state, empty_streak, last_crawled_at "
        "FROM companies_effective WHERE id = ?", (company_id,)).fetchone()
    if not row:
        return None
    state = row["crawl_state"] or "active"
    if err is not None:
        return state

    now = datetime.now()
    stamp = now.isoformat()
    streak = row["empty_streak"] or 0
    sets: dict[str, object] = {"last_crawled_at": stamp}

    open_on_file = conn.execute(
        "SELECT 1 FROM jobs WHERE company_id = ? AND COALESCE(status, 'open') = 'open' "
        "LIMIT 1", (company_id,)).fetchone() is not None
    if n_jobs or open_on_file:
        streak = 0
        sets["empty_streak"] = 0
        sets["last_nonempty_at"] = stamp
        if state == "dormant":                     # the board woke up
            state = "active"
            sets["next_crawl_at"] = None
    else:
        same_day = (row["last_crawled_at"] or "")[:10] == stamp[:10]
        if not same_day:
            streak += 1
            sets["empty_streak"] = streak

    if state != "off" and not row["watch"]:
        if streak >= dormant_after or _offmission_volume(conn, company_id):
            state = "dormant"
            sets["next_crawl_at"] = (now + timedelta(days=dormant_days)
                                     ).isoformat()

    sets["crawl_state"] = state
    apply_update(conn, "companies", "id", company_id, sets)
    return state


def crawlable_companies(conn: sqlite3.Connection, tag: str | None = None) -> list[CompanyRow]:
    """The active companies due for a crawl: everything except the dormant
    rows whose weekly slot has not come round yet. What build_sources and
    sync_status_all fetch, in place of every active row.

    Review candidates are `active = 0`, so they are never fetched -- the
    whole point of the queue is that an unconfirmed guess costs nothing:

    >>> from src.store import mark_pending
    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, mark_pending(
    ...     {"name": "Guess", "ats": "lever", "slug": "guess"}))
    >>> crawlable_companies(conn)
    []

    A capture-only company (ats = CAPTURE_ATS) is active and on the roster,
    but there is no board to fetch -- the person saves its pages by hand --
    so it is never handed to a fetcher, and never earns an empty streak:

    >>> _ = upsert_company(conn, {"name": "Saved By Hand", "ats": CAPTURE_ATS,
    ...                           "careers_url": "https://jobs.x.org/"})
    >>> crawlable_companies(conn)
    []

    A dormant row comes round once its next_crawl_at has passed; 'off' never:

    >>> for n, state, wake in [("Parked", "dormant", "2999-01-01"),
    ...                        ("Due", "dormant", "2000-01-01"), ("Off", "off", None)]:
    ...     _ = upsert_company(conn, {"name": n, "ats": "lever", "slug": n})
    ...     _ = conn.execute("UPDATE companies SET crawl_state=?, next_crawl_at=? "
    ...                      "WHERE name=?", (state, wake, n))
    >>> [c["name"] for c in crawlable_companies(conn)]
    ['Due']

    Notes:
        The filter was Python over every active row until 2026-10-07; the
        SQL agreed with it on all 412,048 (row, time) pairs of the live
        roster, dormant wake times and their neighbours included.
    """
    return get_companies(conn, active_only=True, tag=tag,
                         due_at=datetime.now().isoformat())


def harvestable_companies(conn: sqlite3.Connection) -> list[CompanyRow]:
    """Every company with a fetchable board, for the background harvester:
    active or not, dormant or not, any tag, any mission score. Skipped only
    when there is no board to fetch (capture rows, no ATS, a dead-board or
    no-board miss) or the name is blocklisted.

    >>> from src.store import block_name, mark_pending
    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "Dormant", "ats": "lever",
    ...                           "slug": "d", "crawl_state": "dormant",
    ...                           "next_crawl_at": "2999-01-01"})
    >>> _ = upsert_company(conn, mark_pending(
    ...     {"name": "Guess", "ats": "greenhouse", "slug": "g"}))
    >>> _ = record_miss(conn, "Dead", "board-dead:ultipro", ats="ultipro",
    ...                 slug="x")
    >>> _ = upsert_company(conn, {"name": "Saved", "ats": CAPTURE_ATS,
    ...                           "careers_url": "https://j.x.org/"})
    >>> sorted(c["name"] for c in harvestable_companies(conn))
    ['Dormant', 'Guess']
    >>> _ = block_name(conn, "Guess")
    >>> sorted(c["name"] for c in harvestable_companies(conn))
    ['Dormant']
    """
    # Deferred, and so is review.py's one reach back here, so NEITHER
    # module depends on the other at load time and store/__init__ may
    # import them in any order. The roster and the review queue both
    # know what a company name is; that much they genuinely share.
    from .review import _name_key, blocked_name_keys
    blocked = blocked_name_keys(conn)
    out: list[CompanyRow] = []
    for c in get_companies(conn, active_only=False):
        ats = c.get("ats")
        if not ats or ats == CAPTURE_ATS:
            continue
        # miss_reason families that mean there is nothing at the board's
        # address. Everything else (inactive, dormant, pending review,
        # off-mission, even 'no-local-jobs') still HAS a board, and the
        # harvester pulls it.
        if (c.get("miss_reason") or "").startswith(("board-dead", "no-board-found")):
            continue
        if _name_key(c.get("name") or "") in blocked:
            continue
        out.append(c)
    return out


# Consecutive calendar days a board may carry the harvester's own
# 'fetch-error:harvest' miss before mark_harvested promotes it to
# 'board-dead:<ats>' -- the same family src.ops.repair.RERESOLVE_FAMILIES
# retries and harvestable_companies skips.
HARVEST_DEAD_AFTER_DAYS = 3


def mark_harvested(conn: sqlite3.Connection, company_id: int, n_jobs: int,
                   soft_fail: bool = False, now: datetime | None = None) -> str | None:
    """Stamp one harvest pass's outcome for a board, and run the
    dead-board promotion cycle on its miss_reason.

    `soft_fail` is the caller's own verdict (from the fetch's failure
    count -- see net.http.snapshot_info/fetch_failed, read by
    src.crawl.harvest.harvest_board) that this pass's EMPTY result is a
    board that answered with an error, not a board that genuinely listed
    nothing. `n_jobs` is always 0 when `soft_fail` is set.

    A normal pass (`soft_fail=False`) always stamps `last_harvested_at`
    and the board's true size (`total_job_count`), and `last_nonempty_at`
    too when `n_jobs` is nonzero. A non-empty pass also clears a still-
    pending 'fetch-error:harvest' miss -- the board recovered.

    A soft-failed pass (`soft_fail=True`) stamps only `last_harvested_at`:
    `total_job_count` keeps its last known-good value instead of being
    zeroed by a fetch hiccup. It records 'fetch-error:harvest' as the
    row's miss_reason/miss_at on its FIRST occurrence only -- a miss
    already on the row (any family, including a repeat
    'fetch-error:harvest') is left untouched, so neither `miss_at` nor a
    genuinely different failure is overwritten. A row already carrying
    'fetch-error:harvest' for >= HARVEST_DEAD_AFTER_DAYS days is promoted
    to 'board-dead:<its ats>' instead (and deactivated, the same as a manually
    pruned dead board -- see src.ops.repair.prune_dead_boards's
    deactivate_company call -- which is what lets reresolve_misses pick it
    back up).

    Harvest never touches empty_streak/crawl_state -- those belong to the
    crawl's own dormancy bookkeeping (record_crawl_outcome).

    Returns the 'board-dead:<ats>' reason string when THIS call is the
    one that just promoted the row (so the caller can log a WARNING),
    else None.

    Notes:
        Promotion sets active=0, which is NOT free of side effects: an
        active row's board is also what crawlable_companies (the daily
        crawl's own selection) fetches, since that reads
        get_companies(active_only=True) same as harvest's own
        harvestable_companies does. Deactivating a promoted board
        therefore pulls it out of BOTH: the daily crawl on whatever
        track(s) watch it, and every later harvest pass (already true
        of any 'board-dead'/'no-board-found' row via
        harvestable_companies's no-board check). That is the
        deliberate trade here, for two reasons: (1) _reresolve_candidates
        only ever selects `COALESCE(active, 0) = 0` rows -- exactly as
        record_miss's own inactive-by-construction misses do -- so a
        promoted row that stayed active would never reach
        reresolve_misses at all; (2) this mirrors the existing manual
        path for the same verdict (src.ops.repair.prune_dead_boards's
        deactivate_company on a dead board probe). Three straight days
        of a board answering with nothing is treated as at least as
        strong evidence as that single live probe.

        This does NOT extend to the pre-promotion 'fetch-error:harvest'
        state: an active row keeps that miss_reason (and stays active,
        still crawled and still harvested -- harvestable_companies only
        skips the 'board-dead'/'no-board-found' prefixes) for up to
        HARVEST_DEAD_AFTER_DAYS, its grace period. One place this DOES leak
        into is purely cosmetic: store.miss_counts / the web UI's
        `company_misses` tally count every non-NULL miss_reason
        regardless of `active`, so an active board mid-grace-period
        briefly inflates that dashboard number under the 'fetch-error'
        family -- no code path treats it as unfetchable or unsafe
        while it is active.

    >>> conn = connect(":memory:")
    >>> cid = upsert_company(conn, {"name": "Acme", "ats": "lever",
    ...                             "slug": "acme", "total_job_count": 9})
    >>> mark_harvested(conn, cid, 0, soft_fail=True) is None
    True
    >>> row = get_company(conn, cid)
    >>> row["total_job_count"], row["miss_reason"]
    (9, 'fetch-error:harvest')

    A genuinely empty (no fetch error) pass, by contrast, DOES zero the
    count and never touches miss_reason:

    >>> cid2 = upsert_company(conn, {"name": "Zeta", "ats": "lever",
    ...                              "slug": "z", "total_job_count": 5})
    >>> mark_harvested(conn, cid2, 0)
    >>> row = get_company(conn, cid2)
    >>> row["total_job_count"], row["miss_reason"]
    (0, None)
    """
    # The harvester's own dead-board qualifier: a pass whose board answered
    # with an error and returned no jobs. A generic "fetch-error" miss
    # (src.discovery) means the RESOLUTION attempt raised; this one means a
    # board the roster already trusts kept failing to answer during
    # ordinary harvesting.
    fetch_error = "fetch-error:harvest"
    now_dt = now or datetime.now()
    stamp = now_dt.isoformat()
    sets: dict[str, object] = {"last_harvested_at": stamp}
    if not soft_fail:
        sets["total_job_count"] = n_jobs
        if n_jobs:
            sets["last_nonempty_at"] = stamp
    apply_update(conn, "companies", "id", company_id, sets)

    row = conn.execute("SELECT ats, miss_reason, miss_at FROM companies "
                       "WHERE id=?", (company_id,)).fetchone()
    cur_reason = row["miss_reason"] if row else None
    promoted = None
    miss: dict[str, object] = {}
    if soft_fail:
        if cur_reason is None:
            miss = {"miss_reason": fetch_error, "miss_at": stamp}
        elif cur_reason == fetch_error:
            cutoff = (now_dt - timedelta(days=HARVEST_DEAD_AFTER_DAYS)
                      ).isoformat()
            if (row["miss_at"] or "") <= cutoff:
                promoted = (f"board-dead:{row['ats']}" if row["ats"]
                            else "board-dead")
                miss = {"miss_reason": promoted, "miss_at": stamp, "active": 0}
        # any other family already on the row (no-board-found, ats-
        # unsupported, ...): leave it alone, per record_miss's contract.
    elif n_jobs and cur_reason == fetch_error:
        miss = {"miss_reason": None, "miss_at": None}
    apply_update(conn, "companies", "id", company_id, miss)
    return promoted


def mark_harvest_attempted(conn: sqlite3.Connection, company_id: int,
                           now: datetime | None = None) -> None:
    """Stamp `harvest_attempted_at`: a harvest pass started this board,
    whatever comes of it (src.crawl.harvest.plan's rotation key)."""
    apply_update(conn, "companies", "id", company_id,
                 {"harvest_attempted_at": (now or datetime.now()).isoformat()})


def reactivate_company(conn: sqlite3.Connection, company_id: int) -> None:
    """Undormant one company: back to 'active', streak cleared, no parked
    wake time. The escape hatch for a board the rules retired too eagerly."""
    conn.execute(
        "UPDATE companies SET crawl_state='active', empty_streak=0, "
        "next_crawl_at=NULL WHERE id=?", (company_id,))
    _commit(conn)


def deactivate_company(conn: sqlite3.Connection, company_id: int,
                       note: str | None = None) -> None:
    """Flip one company's `active` switch off, optionally recording why in
    `notes`. The primitive behind src.ops.repair.prune_dead_boards; the
    decision (probe the board, apply the off-mission policy) lives there,
    only the write lives here.

    >>> conn = connect(":memory:")
    >>> cid = upsert_company(conn, {"name": "Gone Co", "ats": "lever",
    ...                             "slug": "gone", "notes": "was fine"})
    >>> deactivate_company(conn, cid, note="deactivated: dead lever board")
    >>> row = get_company(conn, cid)
    >>> row["active"], row["notes"]
    (0, 'deactivated: dead lever board')

    Without a note the existing notes are left alone:

    >>> cid2 = upsert_company(conn, {"name": "Quiet Co", "notes": "keep"})
    >>> deactivate_company(conn, cid2)
    >>> get_company(conn, cid2)["notes"]
    'keep'
    """
    if note is None:
        conn.execute("UPDATE companies SET active=0 WHERE id=?", (company_id,))
    else:
        conn.execute("UPDATE companies SET active=0, notes=? WHERE id=?",
                     (note, company_id))
    _commit(conn)
