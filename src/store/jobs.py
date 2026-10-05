"""The `jobs` table: postings, their track membership, and their scores.

Track membership (jobs.track holds a comma-separated SET, not one name),
the upsert and dedup paths, the harvest triage columns src/crawl/triage.py
writes, and the status/score/ranking reads the digest and the web UI make.

Split out of store/__init__.py alongside companies.py. The two share
nothing: this module never reads the companies table and the roster half
never reads this one. `combined_score` and the track-set helpers are here
because ranking and upsert are their only callers.

Never imports store/__init__ at load time (that module imports this one).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any, cast

from src import config
from src import tags
from src.match.locality import LocationRE
from src.net.util import clean_url
from src.rows import FetchedJob, FitColumns, JobIn, JobRow, RankedJob
from .schema import (_commit, apply_update, as_job, batch,  # noqa: F401 (doctests)
                     connect, dedup_groups, sql, sql_function)


@sql_function("combined_score", 2)
def combined_score(fit: float | None, mission: float | None) -> float | None:
    """Geometric mean sqrt(fit * mission) of the resume-fit and company
    mission scores (both 0..1).

    >>> combined_score(0.25, 0.64)
    0.4
    >>> combined_score(1.0, 1.0)
    1.0

    Floats are compared at a stated precision, never by their full repr —
    the house rule for any numeric doctest:

    >>> round(combined_score(0.9, 0.2), 4)
    0.4243
    >>> round(combined_score(0.5, 0.5), 4)
    0.5

    Those two lines are the point of the geometric mean: it punishes
    imbalance, so a strong fit at a weak-mission company (0.42) ranks below
    a job that is merely solid on both axes (0.50).

    A missing factor is unranked, NOT zero — a job is only scored once both
    axes are known:

    >>> combined_score(None, 0.9) is None
    True
    >>> combined_score(0.9, None) is None
    True

    Negative input is out of domain and yields None rather than a
    ``ValueError`` from ``sqrt`` or a bogus positive from sqrt(-a * -b):

    >>> combined_score(-0.5, 0.5) is None
    True
    >>> combined_score(-0.5, -0.5) is None
    True

    Zero is a legitimate score and stays zero:

    >>> combined_score(0.0, 0.9)
    0.0
    """
    if fit is None or mission is None:
        return None
    if fit < 0 or mission < 0:
        return None
    return math.sqrt(fit * mission)


# --------------------------------------------------------------------------- #
#  Track membership                                                            #
# --------------------------------------------------------------------------- #
#
# jobs.track holds a comma-separated SET of track names, not one name: the
# same posting can belong to several tracks (a neural-company job in your
# area is both local and neural material), and one store now serves every
# track. Same shape as companies.tags, and matched the same way in SQL.

def track_set(value: str | None) -> set[str]:
    """The set of track names in a stored `track` value ('' / None -> set())."""
    return {t.strip() for t in (value or "").split(",") if t.strip()}


def join_tracks(tracks: Iterable[str | None]) -> str | None:
    """Canonical stored form for a set of track names (sorted, comma-joined)."""
    return ",".join(sorted(t for t in tracks if t)) or None


def open_in_track_clause(track: str | None = None, *, alias: str = "",
                         include_closed: bool = False,
                         include_dispositioned: bool = False) -> tuple[list[str], list[Any]]:
    """The baseline "a job that could still surface" filter, as
    (conditions, args) for any query over the jobs table.

    Three things always travel together wherever this population is asked
    for -- track membership (the comma-delimited LIKE that get_companies
    already uses for tags), "not closed", and "not a posting the person has
    already ruled on" (RANKING_EXCLUDED_DISPOSITIONS) -- and ranked_jobs,
    the self-heal pass, the bulk rescore and the deep-verify floor had each
    spelled all three out again.

    `conditions` is a list to AND together; the caller appends its own
    conditions AFTER these and its own args after `args`, so the ``?``
    order lines up. `alias` is the jobs-table alias the query uses (""
    for an unaliased ``FROM jobs``, "j" for ``FROM jobs j``).

    >>> conds, args = open_in_track_clause("local", alias="j")
    >>> conds[0]
    "(',' || COALESCE(j.track,'') || ',') LIKE ?"
    >>> args[0]
    '%,local,%'

    No track means every track, and either flag drops its condition --
    which is how ranked_jobs' `include_closed` / `include_dispositioned`
    are spelled:

    >>> open_in_track_clause(include_closed=True, include_dispositioned=True)
    ([], [])
    >>> conds, _ = open_in_track_clause(include_dispositioned=True)
    >>> conds
    ["COALESCE(status,'open') != 'closed'"]
    """
    p = f"{alias}." if alias else ""
    conds: list[str] = []
    args: list[Any] = []
    if track:
        conds.append(f"(',' || COALESCE({p}track,'') || ',') LIKE ?")
        args.append(f"%,{track},%")
    if not include_closed:
        conds.append(f"COALESCE({p}status,'open') != 'closed'")
    if not include_dispositioned:
        # Deferred, like pipeline.py's one reach back here, so neither
        # module depends on the other at load time (same rule as
        # companies/review). Ranking hides what a person ruled on.
        from .pipeline import RANKING_EXCLUDED_DISPOSITIONS
        ph = ",".join("?" for _ in RANKING_EXCLUDED_DISPOSITIONS)
        conds.append(f"({p}disposition IS NULL OR "
                     f"{p}disposition NOT IN ({ph}))")
        args += list(RANKING_EXCLUDED_DISPOSITIONS)
    return conds, args


# --------------------------------------------------------------------------- #
#  Jobs                                                                        #
# --------------------------------------------------------------------------- #

def job_exists(conn: sqlite3.Connection, job_id: str) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone() is not None


def crawl_seen(conn: sqlite3.Connection, job_id: str) -> bool:
    """Has a CRAWL already handled this posting? True for a row that
    carries a track label -- the crawl stamps one whether it scored the row
    or stored it unscored under a budget guard. A row the harvester stored
    (no track yet) reads as unseen, so the crawl still gates and scores it:

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "h1", "title": "T",
    ...                       "harvested_at": "2026-09-10T01:00:00"})
    >>> job_exists(conn, "h1"), crawl_seen(conn, "h1")
    (True, False)
    >>> _ = upsert_job(conn, {"job_id": "h1", "title": "T", "track": "local"})
    >>> crawl_seen(conn, "h1")
    True
    """
    row = conn.execute("SELECT track FROM jobs WHERE job_id=?",
                       (job_id,)).fetchone()
    return bool(row and (row["track"] or "").strip())


def descriptions_for_company(conn: sqlite3.Connection, company_id: int | None) -> dict[str, str]:
    """{job_id: description} for a company's stored rows that have a body,
    so a crawl can reuse what the harvester already hydrated instead of
    re-fetching every detail page."""
    if not company_id:
        return {}
    return {r["job_id"]: r["description"] for r in conn.execute(
        "SELECT job_id, description FROM jobs WHERE company_id=? "
        "AND length(COALESCE(description,'')) > 0", (company_id,))}


# --------------------------------------------------------------------------- #
#  Harvest triage (src/crawl/triage.py)                                        #
# --------------------------------------------------------------------------- #

# Row verdicts, cheapest gate first. The digest and the pass summary count
# rows by these; `ok` is the only one that puts a row into a track set.
TRIAGE_GATES = ("mission", "title", "anchor", "geo", "exclude", "division",
                "fit")
TRIAGE_OK = "ok"


def triage_pending(conn: sqlite3.Connection, company_id: int | None = None,
                   limit: int | None = None) -> list[JobRow]:
    """Open, company-linked rows no crawl has adopted (no track label) and
    triage has not judged yet -- the harvester's unscored material. A row
    triage looked at but could not hydrate stays NULL, so it comes back
    here next pass.

    >>> from src.store import upsert_company
    >>> conn = connect(":memory:")
    >>> cid = upsert_company(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> for jid, extra in [("h1", {}), ("h2", {"track": "local"}),
    ...                    ("h3", {"status": "closed"}),
    ...                    ("h4", {"triage_status": "title"})]:
    ...     _ = upsert_job(conn, {"job_id": jid, "title": "T",
    ...                           "company_id": cid, **extra})
    >>> record_triage(conn, "h4", "title", "local=title")
    >>> [r["job_id"] for r in triage_pending(conn)]
    ['h1']
    """
    q = ("SELECT j.* FROM open_jobs j "
         "JOIN companies c ON j.company_id = c.id "
         "WHERE j.triage_status IS NULL "
         "AND COALESCE(j.track,'') = ''")
    args: list[Any] = []
    if company_id is not None:
        q += " AND j.company_id = ?"
        args.append(company_id)
    q += " ORDER BY j.company_id, j.first_seen"
    if limit:
        q += " LIMIT ?"
        args.append(int(limit))
    return [as_job(r) for r in conn.execute(q, args).fetchall()]


def record_triage(conn: sqlite3.Connection, job_id: str, status: str, detail: str, *,
                  tracks: Iterable[str] = (), description: str | None = None,
                  geo_mode: str | None = None, remote_signal: str | None = None,
                  scores: FitColumns | None = None, now: datetime | None = None) -> None:
    """Write one row's triage verdict. `tracks` (the track labels the row
    surfaced into) MERGE into the stored set exactly as a crawl's label
    would, so crawl_seen reads the row as handled; `description` fills an
    empty body only; `scores` is a FitResult.as_columns() dict.

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "j", "title": "T", "track": "x"})
    >>> record_triage(conn, "j", "ok", "y=ok", tracks=["y"],
    ...               description="body", scores={"resume_fit_score": 0.5})
    >>> r = conn.execute("SELECT * FROM jobs").fetchone()
    >>> (r["triage_status"], r["triage_detail"], sorted(track_set(r["track"])),
    ...  r["description"], r["resume_fit_score"])
    ('ok', 'y=ok', ['x', 'y'], 'body', 0.5)
    """
    sets: dict[str, Any] = {"triage_status": status, "triage_detail": detail,
                            "triaged_at": (now or datetime.now()).isoformat()}
    if tracks:
        prev = conn.execute("SELECT track FROM jobs WHERE job_id=?",
                            (job_id,)).fetchone()
        merged = track_set(prev["track"] if prev else None) | set(tracks)
        sets["track"] = join_tracks(merged)
    if description:
        sets["description"] = sql("COALESCE(NULLIF(description,''), ?)",
                                  description[:config.MAX_DESC_CHARS])
    if geo_mode:
        sets["geo_mode"] = sql("COALESCE(geo_mode, ?)", geo_mode)
    if remote_signal:
        sets["remote_eligible"] = sql("1")
        sets["remote_signal"] = sql("COALESCE(remote_signal, ?)", remote_signal)
    if scores:
        sets.update((c, scores.get(c)) for c in _SCORE_COLS)
    apply_update(conn, "jobs", "job_id", job_id, sets)


def clear_triage(conn: sqlite3.Connection, job_id: str) -> None:
    """Undo record_triage on one row, so store.triage_pending selects it
    again: every column it writes goes back to NULL except the body, and
    desc_checked_at (the detail retry clock) is left alone.

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "j", "title": "T"})
    >>> record_triage(conn, "j", "ok", "y=ok", tracks=["y"],
    ...               description="body", scores={"resume_fit_score": 0.5})
    >>> clear_triage(conn, "j")
    >>> r = conn.execute("SELECT * FROM jobs").fetchone()
    >>> (r["triage_status"], r["track"], r["resume_fit_score"],
    ...  r["description"])
    (None, None, None, 'body')
    """
    # What record_triage writes besides the body: these and the scores.
    apply_update(conn, "jobs", "job_id", job_id,
                 {c: None for c in ("track", "triage_status", "triage_detail", "triaged_at",
                                    "geo_mode", "remote_eligible", "remote_signal",
                                    *_SCORE_COLS)})


def store_body(conn: sqlite3.Connection, job_id: str, description: str | None,
               location: str | None = None) -> None:
    """Keep a freshly fetched body (and, when the detail page named one,
    the real location) on a row whose verdict is still open, so the next
    pass does not fetch it again. An empty body never blanks a stored one.

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "j", "title": "T",
    ...                       "location": "2 Locations"})
    >>> store_body(conn, "j", "body", "Durham, NC; Remote")
    >>> r = conn.execute("SELECT description, location FROM jobs").fetchone()
    >>> (r["description"], r["location"])
    ('body', 'Durham, NC; Remote')
    """
    conn.execute(
        "UPDATE jobs SET description=COALESCE(NULLIF(?,''), description), "
        "location=COALESCE(NULLIF(?,''), location) WHERE job_id=?",
        ((description or "")[:config.MAX_DESC_CHARS], location or "", job_id))
    _commit(conn)


def mark_desc_checked(conn: sqlite3.Connection, job_id: str,
                      now: datetime | None = None) -> None:
    """Stamp a failed body fetch so the next pass does not retry it at once
    (the same desc_checked_at the backfill ops honour)."""
    conn.execute("UPDATE jobs SET desc_checked_at=? WHERE job_id=?",
                 ((now or datetime.now()).isoformat(), job_id))
    _commit(conn)


def record_probe_outcome(conn: sqlite3.Connection, job_id: str, verified: bool,
                         now: datetime | None = None) -> int:
    """Stamp one closure probe that left the row OPEN and return the row's
    new probe_streak: 0 when the probe `verified` the posting is still
    live, one more than before when it could not tell either way.

    The stamp is the same desc_checked_at mark_desc_checked writes -- it is
    what rotates check_closed_jobs' bounded passes through the backlog --
    so a probe writes one row, not two:

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "j", "title": "T"})
    >>> record_probe_outcome(conn, "j", verified=False)
    1
    >>> record_probe_outcome(conn, "j", verified=False)
    2
    >>> conn.execute("SELECT desc_checked_at IS NOT NULL FROM jobs"
    ...              ).fetchone()[0]
    1

    A probe that finds the posting live resets the streak, so only an
    UNBROKEN run of unverifiable answers ever reaches the give-up count:

    >>> record_probe_outcome(conn, "j", verified=True)
    0

    Notes:
        The companies-side precedent is record_crawl_outcome's
        empty_streak, which counts fruitless crawls of a board the same way
        and parks it dormant. This one never changes a job's status: a row
        nobody can verify is not a row anybody has shown to be closed.
    """
    streak = 0 if verified else (conn.execute(
        "SELECT COALESCE(probe_streak, 0) FROM jobs WHERE job_id=?",
        (job_id,)).fetchone() or [0])[0] + 1
    conn.execute(
        "UPDATE jobs SET desc_checked_at=?, probe_streak=? WHERE job_id=?",
        ((now or datetime.now()).isoformat(), streak, job_id))
    _commit(conn)
    return streak


def triage_counts(conn: sqlite3.Connection, days: float | None = None) -> dict[str, int]:
    """{verdict: n} over triaged rows, optionally only those judged in the
    last `days` days -- the per-gate funnel the digest shows.

    >>> conn = connect(":memory:")
    >>> for jid, st in [("a", "ok"), ("b", "title"), ("c", "title")]:
    ...     _ = upsert_job(conn, {"job_id": jid, "title": "T"})
    ...     record_triage(conn, jid, st, "")
    >>> triage_counts(conn)
    {'ok': 1, 'title': 2}
    """
    q = ("SELECT triage_status AS s, COUNT(*) AS n FROM jobs "
         "WHERE triage_status IS NOT NULL")
    args: list[Any] = []
    if days:
        q += " AND triaged_at >= ?"
        args.append((datetime.now() - timedelta(days=days)).isoformat())
    q += " GROUP BY triage_status"
    rows = conn.execute(q, args).fetchall()
    order = (TRIAGE_OK, *TRIAGE_GATES)
    return {r["s"]: r["n"] for r in sorted(
        rows, key=lambda r: order.index(r["s"]) if r["s"] in order
        else len(order))}


def upsert_job(conn: sqlite3.Connection, j: JobIn, keep_location: bool = False) -> bool:
    """Insert or refresh a job. Returns True if it was new.

    `first_seen` stays stable across re-runs; scores refresh so the stored
    values always reflect the latest scorer. `keep_location=True` (the
    caller's listing named no place) keeps a stored location instead of
    overwriting it; a new row, or one with no location yet, stores what it
    was given.

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "j", "title": "T",
    ...                       "location": "2 Locations"})
    >>> store_body(conn, "j", "body", "Springfield, IL; Remote")
    >>> _ = upsert_job(conn, {"job_id": "j", "title": "T",
    ...                       "location": "2 Locations"}, keep_location=True)
    >>> conn.execute("SELECT location FROM jobs").fetchone()[0]
    'Springfield, IL; Remote'
    >>> _ = upsert_job(conn, {"job_id": "j", "title": "T",
    ...                       "location": "Peoria, IL"})
    >>> conn.execute("SELECT location FROM jobs").fetchone()[0]
    'Peoria, IL'
    """
    now = datetime.now().isoformat()
    url = clean_url(j.get("url"))
    new = not job_exists(conn, j["job_id"])
    if new and url:
        # Same posting arriving under a NEW id scheme — a company's ats/
        # tenant changed (Keebler custom_* -> rippling_*) or a fetcher's id
        # format did (Duke sf__<slug> -> sf_<tenant>_<num>). Re-key the
        # existing row instead of inserting a duplicate: dupes double-rank
        # and double-spend deep-verify (17 such URL pairs in the 2026-08-28
        # store). Title must match too — some custom boards give several
        # DISTINCT postings one landing URL, and those must stay separate
        # rows.
        prev = conn.execute(
            "SELECT job_id, title FROM jobs WHERE url=?",
            (url,)).fetchone()
        if (prev is not None
                and (prev["title"] or "").strip().lower()
                == (j.get("title") or "").strip().lower()):
            conn.execute("UPDATE jobs SET job_id=? WHERE job_id=?",
                         (j["job_id"], prev["job_id"]))
            new = False
    remote = j.get("remote_eligible")
    if remote is not None:
        remote = int(bool(remote))
    # `track` is a SET (see track_set): a posting can legitimately belong to
    # several tracks at once — a neural-company job in your area is both
    # local and neural material — so a second track's crawl ADDS its label
    # instead of stealing the row. Merged here in Python; the ON CONFLICT
    # clause below just writes the union.
    track = j.get("track")
    if track and not new:
        prev = conn.execute("SELECT track FROM jobs WHERE job_id=?",
                            (j["job_id"],)).fetchone()
        track = join_tracks(track_set(prev["track"] if prev else None)
                            | track_set(track))
    conn.execute(
        """INSERT INTO jobs
            (job_id, company_id, company_name, title, url, location, track,
             geo_mode, remote_eligible, remote_signal, anchor_signal,
             description, resume_fit_score, fit_reason,
             fit_domain, fit_function, fit_stack, fit_seniority, fit_gates,
             fit_model, posted_at, first_seen, last_seen, status,
             harvested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             title=excluded.title, url=excluded.url,
             location=CASE WHEN ? AND COALESCE(location, '') != ''
                           THEN location ELSE excluded.location END,
             track=COALESCE(excluded.track, track),
             geo_mode=COALESCE(excluded.geo_mode, geo_mode),
             remote_eligible=COALESCE(excluded.remote_eligible, remote_eligible),
             remote_signal=COALESCE(excluded.remote_signal, remote_signal),
             anchor_signal=COALESCE(excluded.anchor_signal, anchor_signal),
             description=COALESCE(NULLIF(excluded.description,''), description),
             resume_fit_score=COALESCE(excluded.resume_fit_score, resume_fit_score),
             fit_reason=COALESCE(NULLIF(excluded.fit_reason,''), fit_reason),
             fit_domain=COALESCE(excluded.fit_domain, fit_domain),
             fit_function=COALESCE(excluded.fit_function, fit_function),
             fit_stack=COALESCE(excluded.fit_stack, fit_stack),
             fit_seniority=COALESCE(excluded.fit_seniority, fit_seniority),
             fit_gates=COALESCE(excluded.fit_gates, fit_gates),
             fit_model=COALESCE(excluded.fit_model, fit_model),
             posted_at=COALESCE(posted_at, excluded.posted_at),
             last_seen=excluded.last_seen,
             status=excluded.status,
             closed_at=CASE WHEN excluded.status='closed'
                            THEN closed_at ELSE NULL END,
             harvested_at=COALESCE(excluded.harvested_at, harvested_at)""",
        (j["job_id"], j.get("company_id"), j.get("company_name"), j.get("title"),
         url, j.get("location"), track, j.get("geo_mode"),
         remote, j.get("remote_signal"), j.get("anchor_signal"),
         j.get("description"),
         j.get("resume_fit_score"), j.get("fit_reason"),
         j.get("fit_domain"), j.get("fit_function"), j.get("fit_stack"),
         j.get("fit_seniority"), j.get("fit_gates"), j.get("fit_model"),
         j.get("posted_at"), now, now, j.get("status", "open"),
         j.get("harvested_at"), bool(keep_location)),
    )
    _commit(conn)
    return new


# --------------------------------------------------------------------------- #
#  Job status sync, score columns, ranking                                     #
# --------------------------------------------------------------------------- #

@sql_function("norm_title", 1)
def _norm_title(t: str | None) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


@sql_function("norm_url", 1)
def _norm_url(u: str | None) -> str:
    """Scheme/query/fragment/trailing-slash-insensitive URL key."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    return u.split("#", 1)[0].split("?", 1)[0].rstrip("/")


def touch_job(conn: sqlite3.Connection, job_id: str) -> None:
    """Record that a job was just observed live at its source — reopen it and
    refresh last_seen, touching nothing else. For dedupe paths that skip the
    full upsert (e.g. a re-captured LinkedIn card already in the store): the
    sighting must still reset the closed flag and the external-row grace
    clock (see sync_job_statuses), or the next board sync could re-close a
    posting the user just saw live."""
    conn.execute(
        "UPDATE jobs SET status='open', closed_at=NULL, last_seen=?, "
        "probe_streak=0 WHERE job_id=?",
        (datetime.now().isoformat(), job_id))
    _commit(conn)


def sync_job_statuses(conn: sqlite3.Connection, company_id: int | None,
                      fetched_jobs: list[FetchedJob], track: str | None = None,
                      external_grace_days: float = 3, capped: bool = False,
                      now: datetime | None = None) -> tuple[int, int]:
    """Reconcile ONE company's stored jobs against a live board snapshot
    (`fetched_jobs`: dicts with id/title/url, as returned by
    board.company.fetch_company). Rows matched by job_id, URL, or
    normalized title are (re)marked open and their last_seen touched; rows
    that have vanished from the snapshot are marked closed. Returns
    (n_reopened, n_closed).

    Caller contract: only pass a snapshot from a SUCCESSFUL, non-empty fetch
    — fetchers soft-fail to [] (HTTP 404, non-JSON), which is
    indistinguishable from a genuinely emptied board, so an empty snapshot
    must never close anything (this function no-ops on one). Nor may an
    INCOMPLETE one (net.http.snapshot_info()): the caller stores what
    arrived and skips this call.

    Matching depends on where the row's job_id came from:
      * BOARD-NATIVE rows — job_id in the snapshot's own id namespace (same
        "<ats>_<key>_" prefix as some snapshot id) — match by EXACT id only.
        The board is authoritative for its own ids, and boards recycle
        titles across requisitions (Beacon reposts "Algorithm Engineer"
        under a fresh Greenhouse id every cycle), so a title/URL fallback
        would let one live posting shield every dead same-titled req from
        ever closing. Absent id -> closed immediately, UNLESS `capped`
        (below) says otherwise.
      * EXTERNAL rows (LinkedIn captures, NLx, manual --add, legacy ids
        from a retired fetcher) can never id-match, so they match by
        normalized URL or title instead, and are closed only after
        `external_grace_days` without being seen — a manual --add isn't
        insta-closed just because its title doesn't exactly match a board
        row.
      * When `track` is given, only rows of that track are ever CLOSED
        (matched rows are reopened regardless — they're live on the board).

    `capped=True` marks a snapshot truncated by a page cap
    (net.http.note_capped): the pager (the board engine's page walk,
    ...) exhausted its page
    budget without reaching a natural end. Such a pull is an unstable
    WINDOW of the board, not the board itself, so a board-native row
    absent from it is NEVER closed here — not on the first miss, not on
    any later one — regardless of `track`. Reopening still happens
    normally for whatever DOES match, and `now` (default: the clock)
    still stamps last_seen/closed_at for those rows. Closing a capped
    board's vanished rows is ops.check_closed_jobs's job instead: it
    probes each row's own detail URL directly, which is evidence a
    missing page-window slot is not. External rows are unaffected by
    `capped` either way — they were never board-native to begin with, and
    already only close after `external_grace_days`.

    Notes:
        Before 2026-09-18 a capped board-native row closed on its SECOND
        consecutive miss (comparing last_seen against
        MAX(jobs.harvested_at) for the company) rather than never. That
        rule assumed one pass's truncated window was the unlucky
        exception; a live audit that day found 25 Workday/SmartRecruiters
        boards reading their full page budget on EVERY pass (not just one
        unlucky one), so a "second miss" was routinely the same unstable
        window missing the same row twice, not corroborating evidence —
        replaced outright, not narrowed, once the page budget itself grew
        (config.BOARD_MAX_ROWS) to make most of those boards uncapped in
        the first place.

        companies.last_harvested_at was tried as that two-strike boundary
        first and rejected: mark_harvested() stamps it moments AFTER this
        call touches last_seen in the same pass, so every row seen in pass
        N read as older than pass N's own stamp and closed on its first
        miss in pass N+1, with no second-strike grace at all.
        jobs.harvested_at (stamped BEFORE this call, in the same upsert
        batch) was the boundary that actually shipped — kept as the
        record of why, now that the two-strike mechanism itself is gone
        and nothing computes that boundary any more.
    """
    if not company_id or not fetched_jobs:
        return (0, 0)
    ids = {j["id"] for j in fetched_jobs if j.get("id")}
    urls = {u for u in (_norm_url(j.get("url")) for j in fetched_jobs) if u}
    titles = {t for t in (_norm_title(j.get("title")) for j in fetched_jobs) if t}
    # Posting dates piggyback on the sync: every matched row gets its NULL
    # posted_at backfilled from the live snapshot, so the whole store gains
    # real posting dates over normal crawls with zero extra HTTP.
    posted: dict[str, Any] = {}
    for j in fetched_jobs:
        p = j.get("posted_at")
        if not p:
            continue
        for key in (j.get("id"), _norm_url(j.get("url")), _norm_title(j.get("title"))):
            if key:
                posted.setdefault(key, p)
    # "gh_<slug>_123" -> "gh_<slug>_": the id namespace(s) this snapshot
    # covers. First TWO tokens, not rsplit — the per-job tail may itself
    # carry underscores ("wd_amgen_<Title-Slug>_R-250290"). Single-token-tail
    # ids ("custom_<blob>") degrade to a full-id prefix, i.e. those rows only
    # ever close via the grace path — right for the flakiest scraped boards.
    prefixes = tuple({"_".join(i.split("_", 2)[:2]) + "_"
                      for i in ids if "_" in i})
    stamp = (now or datetime.now()).isoformat()
    grace_cutoff = (datetime.now()
                    - timedelta(days=external_grace_days)).isoformat()
    n_reopened = n_closed = 0
    rows = conn.execute(
        "SELECT job_id, url, title, track, status, first_seen, last_seen "
        "FROM jobs WHERE company_id=?", (company_id,)).fetchall()
    for r in rows:
        board_native = r["job_id"].startswith(prefixes)
        present = (r["job_id"] in ids
                   or (not board_native
                       and (_norm_url(r["url"]) in urls
                            or _norm_title(r["title"]) in titles)))
        if present:
            if (r["status"] or "open") != "open":
                n_reopened += 1
            p = (posted.get(r["job_id"]) or posted.get(_norm_url(r["url"]))
                 or posted.get(_norm_title(r["title"])))
            conn.execute(
                "UPDATE jobs SET status='open', closed_at=NULL, last_seen=?, "
                "posted_at=COALESCE(posted_at, ?), probe_streak=0 "
                "WHERE job_id=?",
                (stamp, p, r["job_id"]))
            continue
        if track is not None and track not in track_set(r["track"]):
            continue
        if (r["status"] or "open") == "closed":
            continue
        if board_native:
            # A capped snapshot's window proves nothing about a row it
            # didn't include (see the `capped` paragraph above): never
            # close here. Uncapped, absence from a board-authoritative
            # snapshot IS the evidence — close on this, its first, miss.
            closeable = not capped
        else:
            seen = r["last_seen"] or r["first_seen"] or ""
            closeable = seen < grace_cutoff
        if closeable:
            conn.execute(
                "UPDATE jobs SET status='closed', closed_at=? WHERE job_id=?",
                (stamp, r["job_id"]))
            n_closed += 1
    _commit(conn)
    return (n_reopened, n_closed)


def retire_stopped(conn: sqlite3.Connection, now: datetime | None = None) -> list[tuple[Any, ...]]:
    """Close the open postings of every board the harvester has stopped
    reading (harvestable_companies that config.offmission_inactive calls
    "stopped"), all at one `now` (default: the clock), except the ones
    the user acted on: a disposition, applied_at, followup_at, contact or
    outcome_reason keeps a posting open. Returns the closed rows' (job_id,
    company_id). A board reactivated or re-tiered is walked again, and the
    walk reopens what it still lists (sync_job_statuses).

    >>> from .companies import upsert_company
    >>> conn = connect(":memory:")
    >>> cid = upsert_company(conn, {"name": "Parked", "ats": "lever", "slug": "p",
    ...                             "active": 0, "mission_tier": "other"})
    >>> for j in ("seen", "applied"):
    ...     _ = upsert_job(conn, {"job_id": j, "title": j, "company_id": cid})
    >>> _ = conn.execute("UPDATE jobs SET applied_at='2026-09-01' WHERE job_id='applied'")
    >>> retire_stopped(conn), retire_stopped(conn)
    ([('seen', 1)], [])
    """
    # Deferred, like companies.py's own reach into review.py (see the module doc).
    from .companies import harvestable_companies
    ids = [c["id"] for c in harvestable_companies(conn)
           if config.offmission_inactive(c) == "stopped"]
    acted = " OR ".join(f"COALESCE({col}, '') != ''" for col in (
        "disposition", "applied_at", "followup_at", "contact", "outcome_reason"))
    rows = conn.execute(
        f"UPDATE jobs SET status='closed', closed_at=? "
        f"WHERE company_id IN ({', '.join('?' for _ in ids)}) "
        f"AND COALESCE(status, 'open') != 'closed' AND NOT ({acted}) "
        f"RETURNING job_id, company_id",
        ((now or datetime.now()).isoformat(), *ids)).fetchall()
    _commit(conn)
    return [tuple(r) for r in rows]


# Fit columns written together by the rescore path (see update_job_scores).
_SCORE_COLS = tuple(FitColumns.__annotations__)


def update_job_scores(conn: sqlite3.Connection, job_id: str, cols: FitColumns) -> None:
    """Overwrite only the fit columns for one job (used by rescore). `cols` is a
    FitResult.as_columns() dict; any missing key is written NULL, so passing an
    empty/partial dict clears a stale score (an unscorable row drops out of
    ranking)."""
    apply_update(conn, "jobs", "job_id", job_id,
                 {c: cols.get(c) for c in _SCORE_COLS})


def backfill_axis_columns(conn: sqlite3.Connection) -> int:
    """Populate the per-axis columns (fit_domain/function/stack/seniority,
    fit_gates) from the tag already embedded in fit_reason. Offline, no API.
    Only touches rows that have the tag and a NULL fit_domain, and leaves
    resume_fit_score / fit_reason untouched. Rows with no tag ('no
    description; unscored', or old single-scalar reasons) are skipped."""
    rows = conn.execute(
        "SELECT job_id, fit_reason FROM jobs "
        "WHERE fit_domain IS NULL AND fit_reason LIKE '[dom%'"
    ).fetchall()
    n = 0
    for r in rows:
        # The fit_reason tag summary() writes: "[dom0.45 fun0.72 sta0.55
        # sen0.80 gate:geo+embedded] reason". Gates are '+'-joined in the tag.
        m = re.match(r"\[dom([\d.]+) fun([\d.]+) sta([\d.]+) sen([\d.]+)"
                     r"(?: gate:([^\]]+))?\]", r["fit_reason"] or "")
        if not m:
            continue
        dom, fun, sta, sen, gates = m.groups()
        conn.execute(
            "UPDATE jobs SET fit_domain=?, fit_function=?, fit_stack=?, "
            "fit_seniority=?, fit_gates=? WHERE job_id=?",
            (float(dom), float(fun), float(sta), float(sen),
             (gates.replace("+", ",") if gates else None), r["job_id"]),
        )
        n += 1
    _commit(conn)
    print(f"  {n} of {len(rows)} row(s) backfilled from fit_reason tags.")
    return n


def remote_admitted(row: Mapping[str, Any], remote_mission_floor: float | None) -> bool:
    """Whether an out-of-area REMOTE `row` (a ranked_jobs row) is still
    worth showing in a location-scoped view.

    A watched company qualifies whatever it scores — watch is the one
    human-curated tag, "show me everything at this employer":

    >>> remote_admitted({"company_tags": "local,watch",
    ...                  "mission_score": 0.05}, 0.85)
    True

    Any other company has to reach `remote_mission_floor` on its own
    judged mission score:

    >>> remote_admitted({"mission_score": 0.9}, 0.85)
    True
    >>> remote_admitted({"mission_score": 0.5}, 0.85)
    False

    A company nobody has scored is not admitted (unknown is not a verdict
    in its favour), and a floor of None turns the score arm off entirely,
    leaving watch as the only way in:

    >>> remote_admitted({"mission_score": None}, 0.85)
    False
    >>> remote_admitted({"mission_score": 0.99}, None)
    False

    A multi-division conglomerate never qualifies on score — see
    tests/test_store.py::TestRemoteAdmission, which patches the profile
    policy the check reads.

    Notes:
        The watch list is hand-maintained and lags the data: 8 starred
        companies produced a third of all good-fit rows while 20 unstarred
        ones had produced at least one, and a remote research-engineer
        posting at fit 0.94 fell out of a location-scoped ranking purely
        for want of a star. ranked_jobs applies this server-side; the web
        UI re-applies it per row, because /api/jobs deliberately ships
        every row and gates on the client.
    """
    if tags.has(row.get("company_tags"), tags.WATCH):
        return True
    if remote_mission_floor is None:
        return False
    if config.is_multi_division(row.get("company_name")):
        return False
    mission = row.get("mission_score")
    return mission is not None and mission >= remote_mission_floor


@sql_function("effective_mission", 2)
def _effective_mission(company_name: str | None, mission: float | None) -> float | None:
    """The mission score ranking uses for a job at `company_name`.

    A conglomerate's own mission score is ~0.05 (off-mission overall), but a
    job here already passed the health keyword filter at crawl time -- so
    rank it at the keyword-vetted floor, not the company's score, or its
    combined rank would be sunk unfairly.

    >>> _effective_mission("No Such Conglomerate", 0.3)
    0.3
    >>> _effective_mission("No Such Conglomerate", None) is None
    True
    """
    if config.is_multi_division(company_name):
        return max(mission or 0.0, config.MULTI_DIVISION_MISSION_FLOOR)
    return mission


# ranked_jobs' query, in layers over NARROW rows: a job's description is
# kilobytes, and sorting/windowing 40k of them dragged every body through the
# sorter. `pool` is every row that could still surface, with its company's
# mission; `scored` adds the combined score and applies the mission floor;
# `ranked` numbers each opening's rows best-first and, over the WHOLE group,
# counts them and lists their ids and urls in that same order (the survivor,
# row 1, leads both lists); `picked` is the survivors, in rank order, cut to
# `limit`. Only those rows are joined back to `jobs` for their full columns.
_RANK_SQL = """
WITH pool AS (
  SELECT j.id, j.job_id, j.url, j.company_id, j.company_name, j.title,
         j.resume_fit_score, c.mission_score, employer_tag(c.tags) AS _employer,
         effective_mission(j.company_name, c.mission_score) AS _mission
  FROM jobs j LEFT JOIN companies c ON j.company_id = c.id
  WHERE {pool_where}
), scored AS (
  SELECT *, combined_score(resume_fit_score, _mission) AS combined_score
  FROM pool
  WHERE {min_where}
){collapse}, picked AS (
  SELECT * FROM {source} ORDER BY {order} {limit}
)
SELECT {columns}, c.mission_tier, c.mission_score, c.tags AS company_tags,
       p.combined_score{extra}
FROM picked p JOIN jobs j ON j.id = p.id LEFT JOIN companies c ON j.company_id = c.id
ORDER BY {final_order}"""
# The collapse layer's partition is one "same opening at the same employer"
# group: company_id when a row has one, else the name key (LinkedIn captures,
# jsonld sweep hits and manual --add carry no company_id -- 4 of 119,411 rows
# in the 2026-09-17 live store), else the row's own job_id, which never
# repeats, so a nameless row never collides with every other nameless row.
# Then the normalised title. A company whose tags carry `employer:<key>`
# (tags.EMPLOYER) joins the group of every other row with that key first,
# so two boards of one employer fold the same posting into one row.
_COLLAPSE_SQL = """
, ranked AS (
  SELECT *, ROW_NUMBER() OVER ordered AS _rn,
         COUNT(*) OVER whole AS dup_count,
         json_group_array(job_id) OVER whole AS _dup_ids,
         json_group_array(url) OVER whole AS _dup_urls
  FROM scored
  WINDOW ordered AS (
    PARTITION BY
      CASE WHEN _employer != '' THEN 'employer:' || _employer
           WHEN company_id IS NOT NULL THEN CAST(company_id AS TEXT)
           WHEN name_key(company_name) != '' THEN 'name:' || name_key(company_name)
           ELSE 'job:' || job_id END,
      norm_title(title)
    ORDER BY {order}),
         whole AS (ordered ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
)"""


@sql_function("employer_tag", 1)
def _employer_tag(company_tags: str | None) -> str:
    """tags.employer over a query's column, for ranked_jobs' collapse."""
    return tags.employer(company_tags)


@sql_function("remote_admitted", 4)
def _remote_admitted_cols(company_tags: str | None, company_name: str | None,
                          mission_score: float | None, floor: float | None) -> bool:
    """remote_admitted over a query's columns, for ranked_jobs' geo clause."""
    return remote_admitted({"company_tags": company_tags, "company_name": company_name,
                            "mission_score": mission_score}, floor)


def ranked_jobs(conn: sqlite3.Connection, track: str | None = None, limit: int | None = None,
                location_re: LocationRE | None = None, rank_by: str = "combined",
                allow_geo_modes: Collection[str] | None = None,
                min_mission: float | None = None,
                remote_mission_floor: float | None = None, include_closed: bool = False,
                include_dispositioned: bool = False,
                collapse: bool = True, with_description: bool = False) -> list[RankedJob]:
    """Jobs joined to company mission. `rank_by="combined"` (default) sorts by
    sqrt(resume_fit * company_mission); `rank_by="fit"` sorts by the résumé-fit
    score alone. Use "fit" for a market where every company shares one mission
    tier (e.g. the local health-tech track), so the near-constant mission
    factor doesn't inflate and compress the ranking. `combined_score` is still
    computed either way, so callers can display it. Jobs missing the ranking
    factor fall to the bottom, ordered among themselves by whatever they have.

    `location_re` (a compiled regex) enforces geography at query time,
    independent of the `track` label: a job whose stored location doesn't
    match is excluded from this search but stays in the shared table. This
    is how the local track keeps out-of-area postings out of its results no
    matter which ingest path stamped them `local-tech`.

    `allow_geo_modes` (an iterable of stored `geo_mode` values, e.g.
    {"remote"}) admits rows that fail `location_re` but whose own geo_mode
    already qualifies them — ONLY at companies `remote_admitted` (above)
    trusts with the exception: the watch list, or a mission score at or
    above `remote_mission_floor`. The machine-set sweep tag never earns it —
    auto-probed boards include slug collisions (an EEG company's row that
    actually points at a global AI board), and an unscoped geo_mode
    exception let 87 remote-anywhere rows into a 534-row local ranking.

    `min_mission` drops jobs at companies we positively know are off-mission
    (effective mission below the floor). Needed when ranking by "fit", which
    ignores the mission factor entirely: an off-mission employer's senior ML
    role can otherwise out-rank on-mission work on function/seniority alone
    (a games studio's rec-sys job at fit 0.40 / mission 0.03). Rows with NO
    mission score — unlinked or unscored companies — are KEPT, so the floor
    only removes what has been judged, never what is merely unknown. The
    multi-division floor is applied first, so a conglomerate's keyword-vetted
    job isn't dropped for its parent's low corporate score.

    Jobs marked closed (status='closed' — vanished from their company's
    board, or probed dead; see sync_job_statuses) are excluded unless
    `include_closed=True`. Jobs the user has dispositioned also leave the
    ranking — applied/interviewing live in the digest's pipeline section,
    rejected/dismissed disappear — except 'saved' (shortlisted), which
    stays visible.

    `collapse=True` (the default) then folds rows that are the SAME opening
    at the SAME employer — matched by _COLLAPSE_SQL, i.e. company_id (or a
    name/job_id fallback) plus the normalised title — down to their
    best-ranked survivor, stamped with dup_count/dup_job_ids/dup_urls. It
    runs AFTER every filter and the sort above, so the kept row is the
    best-ranked one, and BEFORE `limit`, so
    `limit=15` always returns 15 visibly-distinct rows rather than 15 raw
    rows that a caller then has to re-collapse and re-trim. These are
    DISTINCT requisitions with distinct URLs (often distinct scores) that
    stay in the store either way — dedup_jobs (slug/url collisions) is the
    only thing that deletes rows; this is a query-time view.

    Every caller defaults to the collapsed view; pass `collapse=False` for
    the raw, one-row-per-posting list — e.g. an audit that needs to see
    every requisition a group folded together.

    Rows carry every `jobs` column except `description`, which is kilobytes
    per row and which no ranking consumer reads (the digest, the web UI and
    the crawl report show fit, title, company and location): loading it for
    40k rows was most of the unlimited call's cost. `with_description=True`
    brings it back.

    Notes:
        A generic title ("Research Technician II") collapses across
        DIFFERENT labs at the SAME employer, because the key has no notion
        of "different team" finer than company + title — the store does not
        record one. That is a real, known blind spot (see the 2026-09-17
        review); it trades a rare false merge of two genuinely different
        openings for fixing the much larger and more common problem, one
        board reposting the identical requisition under several ids/urls.
        dup_job_ids/dup_urls keep every folded row's identity on the
        survivor precisely so a person can still open and tell them apart.

        The census behind the default: 133 company+title groups covered
        442 open tracked rows in the 2026-09-17 store, 43 of them one
        university posting alone, which drowned the digest's and the web
        UI's top ranks in copies of the same opening."""
    conds, args = open_in_track_clause(
        track, alias="j", include_closed=include_closed,
        include_dispositioned=include_dispositioned)
    if location_re is not None:
        # NC_RE is a Python matcher, not a regex: judge each DISTINCT stored
        # location once (~31k of 119k rows) and let SQL keep the accepted set.
        accepted = [loc for (loc,) in conn.execute(
            "SELECT DISTINCT COALESCE(location, '') FROM jobs") if location_re.search(loc)]
        args.append(json.dumps(accepted))
        geo = "COALESCE(j.location, '') IN (SELECT value FROM json_each(?))"
        if allow_geo_modes:
            geo += (" OR (j.geo_mode IN (SELECT value FROM json_each(?)) AND "
                    "remote_admitted(c.tags, j.company_name, c.mission_score, ?))")
            args += [json.dumps(sorted(allow_geo_modes)), remote_mission_floor]
        conds.append(f"({geo})")
    if min_mission is not None:
        args.append(min_mission)
    # Primary sort key per rank_by, then the other factors as tiebreaks; a
    # missing score sorts last via the -1 sentinel (all real scores are
    # >= 0), and id keeps ties in insertion order.
    primary = "resume_fit_score" if rank_by == "fit" else "combined_score"
    keys = dict.fromkeys((primary, "combined_score", "resume_fit_score", "mission_score"))
    def order(p: str = "") -> str:
        return ", ".join(f"COALESCE({p}{k}, -1.0) DESC" for k in keys) + f", {p}id"
    q = _RANK_SQL.format(
        pool_where=" AND ".join(conds) or "1", order=order(), final_order=order("p."),
        min_where="1" if min_mission is None else "(_mission IS NULL OR _mission >= ?)",
        collapse=_COLLAPSE_SQL.format(order=order()) if collapse else "",
        source="ranked WHERE _rn = 1" if collapse else "scored",
        extra=", p.dup_count, p._dup_ids, p._dup_urls" if collapse else "",
        limit="LIMIT ?" if limit else "",
        columns=", ".join(f"j.{r['name']}" for r in conn.execute("PRAGMA table_info(jobs)")
                          if with_description or r["name"] != "description"))
    if limit:
        args.append(int(limit))
    rows = [dict(r) for r in conn.execute(q, args)]     # RankedJob, once the dup_* keys are set
    if collapse:
        for r in rows:
            r["dup_job_ids"] = tuple(json.loads(r.pop("_dup_ids"))[1:])
            r["dup_urls"] = tuple(json.loads(r.pop("_dup_urls"))[1:])
    return cast(list[RankedJob], rows)


# Which of several rows for one posting survives, best first: a dispositioned
# row, then an open one, then the EARLIEST first_seen (ISO strings sort by
# time, and a row with none sorts before every dated one), then the oldest row.
_SURVIVOR_ORDER = """disposition IS NULL, COALESCE(NULLIF(status, ''), 'open') != 'open',
                     COALESCE(first_seen, ''), id"""


def same_posting(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Whether two job rows name one posting: the same URL modulo scheme,
    query and fragment (_norm_url), and the same normalized title.

    >>> same_posting({"url": "https://x.test/j/1?src=a", "title": "Data  Eng"},
    ...              {"url": "http://x.test/j/1/", "title": "data eng"})
    True
    >>> same_posting({"url": "https://x.test/j/1", "title": "Data Eng"},
    ...              {"url": "https://x.test/j/1", "title": "Lab Tech"})
    False
    """
    key = _posting_key(a)
    return all(key) and key == _posting_key(b)


@sql_function("same_posting", 4)
def _same_posting_cols(url_a: str | None, title_a: str | None,
                       url_b: str | None, title_b: str | None) -> bool:
    """same_posting over a query's columns, for the SQL that groups rows."""
    return same_posting({"url": url_a, "title": title_a}, {"url": url_b, "title": title_b})


def _posting_key(r: Mapping[str, Any]) -> tuple[str, str]:
    """(_norm_url, _norm_title) of a job row: its identity across id schemes."""
    return _norm_url(r.get("url")), _norm_title(r.get("title"))


def merge_jobs(conn: sqlite3.Connection, row_ids: Collection[int], job_id: str) -> int:
    """Fold the job rows `row_ids` (one posting stored under several ids)
    into one row named `job_id`; returns its row id. The survivor is
    dedup_jobs' (_SURVIVOR_ORDER) and keeps its values, `closed_at` with
    its status; a field it lacks comes from the others, `first_seen` is
    the earliest, `description` the longest and `track` every track; the
    others are deleted. Rows that are not one posting (same_posting)
    raise ValueError, nothing written.

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "old_7", "title": "T", "url": "u",
    ...                       "track": "a", "resume_fit_score": 0.4})
    >>> _ = upsert_job(conn, {"job_id": "new_x_7", "title": "T", "url": "u?src=a",
    ...                       "track": "b", "description": "body"})
    >>> ids = [r["id"] for r in conn.execute("SELECT id FROM jobs")]
    >>> _ = merge_jobs(conn, ids, "new_x_7")
    >>> [tuple(r) for r in conn.execute("SELECT job_id, track, resume_fit_score, "
    ...                                 "description FROM jobs")]
    [('new_x_7', 'a,b', 0.4, 'body')]
    """
    ph = ",".join("?" for _ in row_ids)
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM jobs WHERE id IN ({ph}) ORDER BY {_SURVIVOR_ORDER}", tuple(row_ids))]
    keep, losers = rows[0], rows[1:]
    if not all(same_posting(keep, l) for l in losers):
        raise ValueError(f"job rows {sorted(row_ids)} are not one posting")
    fill: dict[str, Any] = {}
    for col, mine in keep.items():
        if col in ("id", "job_id", "closed_at"):
            continue
        vals = [mine] + [l[col] for l in losers]
        if col == "first_seen":
            v = min((x for x in vals if x), default=None)
        elif col == "description":
            v = max(vals, key=lambda x: len(x or ""))
        elif col == "track":
            v = join_tracks(set().union(*(track_set(x) for x in vals)))
        else:
            v = next((x for x in vals if x not in (None, "")), mine)
        if v != mine:
            fill[col] = v
    conn.executemany("DELETE FROM jobs WHERE id=?", [(l["id"],) for l in losers])
    apply_update(conn, "jobs", "id", keep["id"], {**fill, "job_id": job_id})
    return cast(int, keep["id"])


def dedup_jobs(conn: sqlite3.Connection) -> int:
    """Collapse job rows that are the SAME posting under different ids: same
    company, same URL modulo scheme/query/fragment (_norm_url), same
    normalized title. upsert_job's re-key only catches an EXACT URL match,
    so the same posting emitted with a different query string under a
    second id namespace slips past it, double-ranks and double-spends
    deep-verify.

    Two guards keep this from eating distinct postings. Title must match —
    some custom boards give several DISTINCT postings one landing URL (see
    upsert_job). And the ids' per-posting tail (the board's own requisition
    number, "..._42453") must match too: Greenhouse companies whose stored
    URL is a shared careers landing page (butterflynetwork.com/careers?
    gh_jid=N) reduce to one URL for every job, and a title reposted under a
    fresh requisition (a second office, a re-opened req) is a separate
    posting, not a duplicate.

    Keeps, per group: a dispositioned row over an undispositioned one, then
    an open row over a closed one, then the earliest first_seen (the row
    whose history is longest). Returns the number of rows deleted.

    Notes:
        The 2026-09-01 store held 12 SAS pairs (iCIMS `?in_iframe=1` vs
        `?hub=9&in_iframe=1`). The dry run without the requisition guard
        would have merged three Butterfly Network pairs.
    """
    # The group is (company, posting key, requisition tail): the tail is the
    # id after its last "_" (the whole id when it has none), taken by
    # stripping the id's non-"_" characters off its right end. Members come
    # back best-first (_SURVIVOR_ORDER), groups in first-row order.
    groups: dict[int, list[dict[str, Any]]] = {}
    for r in conn.execute(f"""
            WITH keyed AS (
              SELECT id, job_id, title, disposition, status, first_seen, company_id,
                     norm_url(url) AS u, norm_title(title) AS t,
                     substr(job_id, length(rtrim(job_id, replace(job_id, '_', ''))) + 1) AS tail
              FROM jobs WHERE company_id IS NOT NULL AND url IS NOT NULL AND url != ''
            ), ranked AS (
              SELECT *, ROW_NUMBER() OVER ordered AS rn, COUNT(*) OVER same AS n,
                     MIN(id) OVER same AS g
              FROM keyed WHERE u != '' AND t != ''
              WINDOW same AS (PARTITION BY company_id, u, t, tail),
                     ordered AS (same ORDER BY {_SURVIVOR_ORDER})
            )
            SELECT job_id, title, g, rn FROM ranked WHERE n > 1 ORDER BY g, rn"""):
        groups.setdefault(r["g"], []).append(dict(r))

    return dedup_groups(
        conn, "jobs", "job_id", groups, rank=lambda r: r["rn"],
        describe=lambda keep, losers: (
            f"{(keep['title'] or '')[:40]:40} kept {keep['job_id'][:28]}"
            f" <- dropped {', '.join(l['job_id'][:28] for l in losers)}"))
