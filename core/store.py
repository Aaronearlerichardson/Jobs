"""
One SQLite store (data/jobs.db) shared by every track: a `companies` table
(with a cached mission score and scope tags) and a `jobs` table (per-job
scores, dedup state, track membership). `jobs.track` is a comma-separated
SET, so a posting that belongs to two tracks is ONE row visible to both —
see track_set() and the LIKE-based filters that read it.

Design (merged from both development tracks):
  * The company row carries the mission judgment once, so individual jobs
    inherit it instead of paying a per-job mission LLM call — "the company
    list simplifies the job list."  (local-clinical insight)
"""

import math
import re
import sqlite3
from datetime import datetime, timedelta

import config

from . import tags


def combined_score(fit, mission):
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
#  Schema                                                                      #
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id             INTEGER PRIMARY KEY,
    name           TEXT UNIQUE NOT NULL,
    ats            TEXT,              -- greenhouse|lever|ashby|workday|...
    slug           TEXT,              -- board slug (non-workday)
    wd_tenant      TEXT,              -- workday triple
    wd_pod         INTEGER,
    wd_site        TEXT,
    careers_url    TEXT,
    local_job_count INTEGER DEFAULT 0, -- openings inside your [locality]
    total_job_count INTEGER DEFAULT 0,
    mission_tier   TEXT,              -- a tier name from profile [mission]
    mission_score  REAL,              -- 0..1 (alignment with what you care about)
    mission_reason TEXT,
    tags           TEXT,              -- comma scope tokens; see core/tags.py
    source         TEXT,              -- how it was discovered
    active         INTEGER DEFAULT 1, -- crawl this company?
    last_probed    TEXT,
    notes          TEXT,
    created_at     TEXT,              -- first time this row was inserted
    miss_reason    TEXT,              -- why it is not crawlable (see MISS_REASONS)
    miss_at        TEXT               -- when that failure was last recorded
);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY,
    job_id         TEXT UNIQUE NOT NULL,  -- source-stable id
    company_id     INTEGER REFERENCES companies(id),
    company_name   TEXT,
    title          TEXT,
    url            TEXT,
    location       TEXT,
    track          TEXT,                  -- comma-separated SET of track names
    geo_mode       TEXT,                  -- onsite|remote
    remote_eligible INTEGER,              -- 1 when the remote filter passed
    remote_signal  TEXT,                  -- phrase/hint that marked it remote
    anchor_signal  TEXT,                  -- the CORE keyword that anchored it
    description    TEXT,
    desc_checked_at TEXT,                 -- last FAILED description backfill
                                          -- attempt (ops.backfill_* skip
                                          -- recently-checked rows)
    resume_fit_score REAL,
    fit_reason     TEXT,
    first_seen     TEXT,
    last_seen      TEXT,
    status         TEXT DEFAULT 'open'   -- open|closed (see sync_job_statuses)
);

-- Company names a person rejected from the review queue. Keyed by the
-- normalized name (see _name_key), so one rejected spelling blocks the rest.
CREATE TABLE IF NOT EXISTS name_blocklist (
    key      TEXT PRIMARY KEY,
    name     TEXT,              -- the spelling that was rejected
    reason   TEXT,
    added_at TEXT
);
"""

# Created after _ensure_columns: on a pre-merge DB the jobs table exists
# without `track`, so these must not run before the column migrations.
_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS ix_jobs_track   ON jobs(track);
"""

# Columns added after a table's first release: additive, idempotent
# migrations so existing DBs (e.g. an old local_tech.db) upgrade in place.
_MIGRATIONS = {
    "companies": {
        "tags": "TEXT",
        # No DEFAULT, unlike _SCHEMA's fresh-DB declaration: ADD COLUMN with a
        # default writes that default into every existing row, which would
        # make the _RENAMED_COLUMNS copy below (guarded on IS NULL) a no-op
        # and silently drop the counts inherited from nc_job_count.
        "local_job_count": "INTEGER",   # was nc_job_count
        # When the row was first inserted. Deliberately NOT backfilled on an
        # existing DB: we do not know when a pre-migration row arrived, and a
        # backfill would invent a roster-growth spike at migration time. NULL
        # reads as "predates the column" (see roster_growth).
        "created_at":  "TEXT",
        # Why a candidate is not crawlable, and when we last found that out.
        # See MISS_REASONS; rows carrying these are always active=0.
        "miss_reason": "TEXT",
        "miss_at":     "TEXT",
        # Crawl scheduling (record_crawl_outcome / crawlable_companies).
        # 181 of 300 active companies had never produced a single job yet
        # were fetched on every crawl, and a handful of huge off-mission
        # boards (a state health agency: 663 local rows, best fit 0.15)
        # burned most of the run. `crawl_state` NULL reads as 'active', so
        # existing rows need no backfill.
        "crawl_state":      "TEXT",      # active|dormant|off (NULL=active)
        "empty_streak":     "INTEGER",   # consecutive empty DAYS
        "last_crawled_at":  "TEXT",
        "last_nonempty_at": "TEXT",
        "next_crawl_at":    "TEXT",      # dormant rows wake at/after this
    },
    "jobs": {
        "track":           "TEXT",
        "remote_eligible": "INTEGER",
        "remote_signal":   "TEXT",
        "anchor_signal":   "TEXT",   # was neural_signal
        # Per-axis fit sub-scores (core/fit.py). resume_fit_score stays
        # the combined scalar; these expose the breakdown for querying/sorting.
        "fit_domain":      "REAL",
        "fit_function":    "REAL",
        "fit_stack":       "REAL",
        "fit_seniority":   "REAL",
        "fit_gates":       "TEXT",   # comma-joined tripped gate names, or NULL
        # Model id that wrote the current score (NULL before this column
        # existed). verify_top re-verifies a finalist unless fit_model is
        # the CURRENT verify model, so a model change re-reads the top N once.
        "fit_model":       "TEXT",
        # When status flipped to 'closed' (NULL while open). Set by
        # sync_job_statuses / set_job_status, cleared on reopen.
        "closed_at":       "TEXT",
        # Real posting date from the board (YYYY-MM-DD; first-known wins).
        # first_seen is when WE noticed it; posted_at is when it went up.
        "posted_at":       "TEXT",
        # The user's decision on this job (see DISPOSITIONS): drives ranking
        # exclusion, the digest pipeline section, and few-shot calibration.
        "disposition":      "TEXT",
        "disposition_note": "TEXT",
        "disposition_at":   "TEXT",
        # Last FAILED description-backfill attempt. The backfill ops skip
        # rows checked in the last few days: a posting that has dropped off
        # its board never matches, and without this stamp every rerun
        # re-fetched the same boards to fail on the same rows ("0 of 18
        # backfilled" three runs in a row, 2026-08-28 session logs).
        "desc_checked_at":  "TEXT",
        # Application-pipeline tracking (see update_pipeline_fields,
        # conversion_report, followups_due). applied_at is stamped the FIRST
        # time a row is marked applied and never again: a later
        # interviewing/rejected must not move the date the application went
        # out, or every elapsed-time question loses its clock. The other four
        # are user-entered; `referral` is 0/1, `outcome_reason` one of
        # OUTCOME_REASONS.
        "applied_at":       "TEXT",
        "followup_at":      "TEXT",   # YYYY-MM-DD, the next nudge
        "contact":          "TEXT",   # recruiter / hiring manager / referrer
        "referral":         "INTEGER",
        "outcome_reason":   "TEXT",
    },
}

# The user's recorded decision on a job. `saved` = shortlisted, still shown
# in ranking; the rest leave the ranking: applied/interviewing move to the
# digest's pipeline section, rejected/dismissed disappear (and dismissed
# rows become negative few-shot examples for the fit scorer — 
# fit.py reads them, so a --note saying WHY is worth writing).
DISPOSITIONS = ("saved", "applied", "interviewing", "rejected", "dismissed")
RANKING_EXCLUDED_DISPOSITIONS = ("applied", "interviewing", "rejected", "dismissed")

# A live application: one that went out and has not come back. followups_due
# only chases these, and conversion_report counts everything else as closed.
LIVE_DISPOSITIONS = ("applied", "interviewing")

# An application that actually went out, however it ended. The denominator
# conversion_report divides by; `saved` and `dismissed` never applied.
APPLIED_DISPOSITIONS = ("applied", "interviewing", "rejected")

# The user-editable application-tracking columns. update_pipeline_fields
# writes these and nothing else, so an API caller cannot reach `disposition`
# (which has its own validated path) or any scorer-owned column through it.
PIPELINE_FIELDS = ("followup_at", "contact", "referral", "outcome_reason")

# How an application ENDED, as a closed vocabulary rather than free text: the
# free-text note already exists for nuance, and a fixed set is what lets
# conversion_report tell "never answered" from "interviewed and lost".
OUTCOME_REASONS = ("no-response", "rejected-screen", "rejected-interview",
                   "withdrew", "closed", "other")

# The resume_fit_score bands conversion_report groups by: (name, low, high),
# half-open on the high side, ordered low to high.
FIT_BANDS = (("low", 0.0, 0.4), ("mid", 0.4, 0.6), ("high", 0.6, 1.01))

# Columns whose CONTENT lives on under a new, field-neutral name: the old
# ones were named for one user's search ("neural" anchors, "nc" for the local
# region). new -> old; _ensure_columns copies old values across before the
# old column is dropped, so no history is lost on an existing DB.
_RENAMED_COLUMNS = {
    "jobs":      {"anchor_signal": "neural_signal"},
    "companies": {"local_job_count": "nc_job_count"},
}

# Columns retired for good. Dropped idempotently on connect so existing DBs
# (which keep old columns under CREATE TABLE IF NOT EXISTS) shed them too.
# mission/tech_bar_score became company-level after unification and
# hq_location was never populated — all three were 100% NULL. The last two
# are the _RENAMED_COLUMNS sources, dropped only after their copy runs.
_DROPPED_COLUMNS = {
    "jobs": ("mission", "tech_bar_score", "neural_signal"),
    "companies": ("hq_location", "nc_job_count"),
}


def _ensure_columns(conn):
    # Concurrency-tolerant: the web UI opens several connections to the same
    # DB at once (one per API request), and on a DB this process hasn't
    # migrated yet they all read PRAGMA table_info before any ALTER lands —
    # every loser then raises "duplicate column name" (or "no such column"
    # for drops). Both mean "another connection already did it": skip.
    for table, cols in _MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
    # Carry renamed columns' values across BEFORE the old ones are dropped.
    # Only fills rows the new column hasn't got a value for, so re-running is
    # a no-op and a re-crawl's fresh value is never overwritten by stale data.
    for table, renames in _RENAMED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for new, old in renames.items():
            if new in existing and old in existing:
                conn.execute(f"UPDATE {table} SET {new}={old} "
                             f"WHERE {new} IS NULL AND {old} IS NOT NULL")
    for table, cols in _DROPPED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
                except sqlite3.OperationalError as e:
                    if "no such column" not in str(e).lower():
                        raise
    conn.commit()


def _migrate_tags(conn):
    """Rewrite retired company scope-tag tokens in place (core/tags.py).

    The tags started out named after one user's search ('nc_local', 'neural')
    and now say what they DO ('local', 'sweep'). Reads tolerate the old names
    via tags.canonical(), but rewriting the stored tokens keeps SQL tag
    filters — which match the literal token — honest. Idempotent, and done in
    Python rather than SQL string surgery so a row that somehow holds both a
    legacy name and its replacement collapses to one token instead of two.
    Costs one scan of a table with hundreds of rows, not millions.
    """
    if not tags.ALIASES:
        return
    where = " OR ".join(["(',' || tags || ',') LIKE ?"] * len(tags.ALIASES))
    rows = conn.execute(
        f"SELECT id, tags FROM companies WHERE tags IS NOT NULL AND ({where})",
        tuple(f"%,{legacy},%" for legacy in tags.ALIASES)).fetchall()
    for row in rows:
        conn.execute("UPDATE companies SET tags=? WHERE id=?",
                     (tags.join(tags.parse(row["tags"])), row["id"]))
    conn.commit()


def connect(path=None):
    conn = sqlite3.connect(path or config.STORE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    _ensure_columns(conn)
    _migrate_tags(conn)
    conn.executescript(_INDEXES)
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
#  Track membership                                                            #
# --------------------------------------------------------------------------- #
#
# jobs.track holds a comma-separated SET of track names, not one name: the
# same posting can belong to several tracks (a neural-company job in your
# area is both local and neural material), and one store now serves every
# track. Same shape as companies.tags, and matched the same way in SQL.

def track_set(value):
    """The set of track names in a stored `track` value ('' / None -> set())."""
    return {t.strip() for t in (value or "").split(",") if t.strip()}


def join_tracks(tracks):
    """Canonical stored form for a set of track names (sorted, comma-joined)."""
    return ",".join(sorted(t for t in tracks if t)) or None


# SQL fragment + arg for "this row belongs to track ?" — the comma-delimited
# LIKE that get_companies already uses for tags.
_TRACK_MATCH_SQL = "(',' || COALESCE(j.track,'') || ',') LIKE ?"


def _track_match_arg(track):
    return f"%,{track},%"


# --------------------------------------------------------------------------- #
#  Companies                                                                   #
# --------------------------------------------------------------------------- #

_COMPANY_COLS = (
    "name", "ats", "slug", "wd_tenant", "wd_pod", "wd_site", "careers_url",
    "local_job_count", "total_job_count", "mission_tier",
    "mission_score", "mission_reason", "tags", "source", "active",
    "last_probed", "notes", "created_at", "miss_reason", "miss_at",
)

# Columns an upsert may write on INSERT but must never overwrite on UPDATE:
# created_at is the row's birth stamp, so a re-probe of a known company must
# leave it (and a legacy NULL) alone.
_INSERT_ONLY_COLS = ("created_at",)


def upsert_company(conn, c):
    """Insert or update a company by name. `c` is a dict of column->value.

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
    """
    c = {**c, "last_probed": c.get("last_probed") or datetime.now().isoformat()}
    c.setdefault("created_at", datetime.now().isoformat())
    # Drop None-valued keys: an upsert must never erase an existing value
    # (e.g. a failed/keyless mission-scoring pass writing mission_score=None
    # over a previously scored company). Inserts still get NULL defaults.
    c = {k: v for k, v in c.items() if v is not None}
    old = conn.execute("SELECT tags FROM companies WHERE name=?",
                       (c["name"],)).fetchone()
    if old and old["tags"]:
        merged = set(t for t in old["tags"].split(",") if t)
        merged |= set(t for t in (c.get("tags") or "").split(",") if t)
        c["tags"] = ",".join(sorted(merged))
    cols = [k for k in _COMPANY_COLS if k in c]
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{k}=excluded.{k}" for k in cols
                        if k != "name" and k not in _INSERT_ONLY_COLS)
    conn.execute(
        f"INSERT INTO companies ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(name) DO UPDATE SET {updates}",
        [c[k] for k in cols],
    )
    if c.get("ats"):
        conn.execute("UPDATE companies SET miss_reason=NULL, miss_at=NULL "
                     "WHERE name=? AND miss_reason IS NOT NULL", (c["name"],))
    conn.commit()
    row = conn.execute("SELECT id FROM companies WHERE name=?", (c["name"],)).fetchone()
    return row["id"] if row else None


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
    # no-board-found qualifiers (discovery.sniffer.diagnose_no_board):
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


def miss_family(reason):
    """The family part of a miss reason: the token before any ':' qualifier.

    >>> miss_family("no-local-jobs")
    'no-local-jobs'
    >>> miss_family("ats-unsupported:ukg")
    'ats-unsupported'
    >>> miss_family(None)
    ''
    """
    return (reason or "").split(":", 1)[0]


def record_miss(conn, name, reason, **fields):
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
    """
    row = conn.execute("SELECT active FROM companies WHERE name=?",
                       (name,)).fetchone()
    if row and row["active"]:
        return False
    now = datetime.now().isoformat()
    upsert_company(conn, {**fields, "name": name, "active": 0,
                          "miss_reason": reason, "miss_at": now})
    # upsert_company clears the miss columns whenever an `ats` is written (a
    # row with a board is a company) -- but here the ats is part of the miss
    # record itself ("board-dead" knows which board died), so put them back.
    conn.execute("UPDATE companies SET miss_reason=?, miss_at=? WHERE name=?",
                 (reason, now, name))
    conn.commit()
    return True


def miss_counts(conn):
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
    tally = {}
    for r in rows:
        fam = miss_family(r["miss_reason"])
        tally[fam] = tally.get(fam, 0) + 1
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))


def recent_miss_names(conn, days=14):
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
    cutoff = (datetime.now() - timedelta(days=int(days))).isoformat()
    return {r["name"] for r in conn.execute(
        "SELECT name FROM companies WHERE miss_reason IS NOT NULL "
        "AND miss_at IS NOT NULL AND miss_at >= ?", (cutoff,)).fetchall()}


def roster_growth(conn, days=7):
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
    cutoff = (datetime.now() - timedelta(days=int(days))).isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM companies "
        "WHERE created_at >= ? AND miss_reason IS NULL",
        (cutoff,)).fetchone()[0]


def prune_dead_boards(conn, max_workers=12, deactivate_offmission=False):
    """Deactivate active companies whose JSON-API ATS board no longer resolves
    (a hard 404/error — the source of the crawl's `HTTP 404` spam), and
    optionally off-mission `other`-tier companies (excluding multi-division).
    Only ATSes whose board endpoint cleanly distinguishes "exists" (200)
    from "dead" (404) are probed. Returns (n_dead, n_offmission)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import config
    from discovery.probes import (probe_greenhouse, probe_lever,
                                   probe_ashby, probe_bamboohr)

    def _ultipro_alive(slug):
        # Not discovery.probes.probe_ultipro: its ok flag means "has jobs",
        # which would prune a live-but-currently-empty board. Dead here
        # means the board REQUEST fails (the 404 spam three roster rows
        # produced in every 2026-08-28 crawl log); an empty listing is
        # alive.
        from scrapers.fetchers.ultipro import parse_board
        try:
            return (True, len(parse_board(slug)))
        except Exception:
            return (False, 0)

    PROBE = {"greenhouse": probe_greenhouse, "lever": probe_lever,
             "ashby": probe_ashby, "bamboohr": probe_bamboohr,
             "ultipro": _ultipro_alive}

    rows = [c for c in get_companies(conn, active_only=True)
            if c.get("ats") in PROBE and c.get("slug")]

    def _check(c):
        ok, _ = PROBE[c["ats"]](c["slug"])
        return c, ok

    dead = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed({ex.submit(_check, c): c for c in rows}):
            c, ok = fut.result()
            if not ok:
                dead.append(c)
    for c in dead:
        conn.execute("UPDATE companies SET active=0, notes=? WHERE id=?",
                     (f"deactivated: dead {c['ats']} board '{c['slug']}'", c["id"]))
        print(f"    [dead]  {c['name'][:30]:30} {c['ats']:10} {c['slug']}")

    n_off = 0
    if deactivate_offmission:
        # Watched companies are exempt: a watch tag is the user deliberately
        # keeping an off-mission employer (e.g. a defense DSP shop) crawled.
        off = [c for c in get_companies(conn, active_only=True)
               if c.get("mission_tier") == "other"
               and not config.is_multi_division(c.get("name"))
               and "watch" not in (c.get("tags") or "").split(",")]
        for c in off:
            conn.execute("UPDATE companies SET active=0 WHERE id=?", (c["id"],))
            print(f"    [other] {c['name'][:30]:30} {c['ats'] or '?':10} "
                  f"mission_score={c.get('mission_score')}")
        n_off = len(off)
    conn.commit()
    return len(dead), n_off


# --------------------------------------------------------------------------- #
#  Capture-only companies                                                      #
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

# Multi-tenant hosts. A page there says which BOARD it is, not which company
# owns it, so company_by_host trusts a domain-level match against a roster
# row's careers_url only on company-owned hosts; on these it insists on the
# board's own path.
_SHARED_HOST_RE = re.compile(
    r"myworkdayjobs|greenhouse\.io|lever\.co|ashbyhq|smartrecruiters|icims|"
    r"taleo|bamboohr|jazzhr|applytojob|paylocity|workable|polymer\.co|"
    r"gusto\.com|rippling|breezy|recruitee|teamtailor|jobvite|ultipro|"
    r"successfactors|peopleadmin|linkedin|indeed|glassdoor|ziprecruiter", re.I)


def _split_url(url):
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


def _board_prefix(path):
    """The first path segment of a careers URL, the piece that names a tenant
    on a shared host ('/axoft/40863' -> '/axoft'); '' for a bare origin."""
    seg = path.strip("/").split("/")[0] if path else ""
    return f"/{seg}" if seg else ""


def company_by_host(conn, url):
    """The roster company whose careers_url (or URL-shaped slug) claims the
    host of `url`, or None. The manual capture path asks this so a page the
    person saved from an employer's own careers site lands under that
    employer's EXISTING row -- id, name, mission score and all -- instead of
    minting a new company from whatever name the page text yields.

    An exact host match wins, and a sibling host on the same company-owned
    domain is accepted too (careers sites live on jobs./careers. subdomains
    while the roster usually holds the www. site):

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
    domain = ".".join(host.split(".")[-2:])
    shared = bool(_SHARED_HOST_RE.search(host))
    sibling = None
    for c in conn.execute("SELECT * FROM companies ORDER BY id").fetchall():
        c = dict(c)
        for cand in (c.get("careers_url"), c.get("slug")):
            if not cand or "." not in str(cand):
                continue
            if not re.match(r"https?://", str(cand), re.I):
                cand = f"https://{cand}"
            chost, cpath = _split_url(cand)
            if not chost:
                continue
            if chost == host:
                prefix = _board_prefix(cpath) if shared else ""
                if not prefix or path.lower().startswith(prefix.lower()):
                    return c
            elif (not shared and sibling is None
                  and ".".join(chost.split(".")[-2:]) == domain):
                sibling = c
    return sibling


def board_key(r):
    """The identity of a company row's BOARD, independent of its name: the
    Workday triple, the (ats, slug) pair, or for careers_url-keyed ATSes the
    URL itself. None when the row has no resolvable board. Shared by
    dedup_companies (merging after the fact) and company_by_board (refusing
    the duplicate before it lands).

    careers_url-keyed ATSes: their slug is a shared datacenter host
    (SuccessFactors "performancemanagerN" serves many tenants) or absent,
    and the careers_url IS the board identity. Keying these on slug merged
    Bayer into Sonova (both performancemanager5).

    >>> board_key({"ats": "workday", "wd_tenant": "redhat", "wd_pod": 5, "wd_site": "jobs"})
    ('workday', 'redhat', 5, 'jobs')
    >>> board_key({"ats": "icims", "slug": "globalcareers-sas", "wd_tenant": None})
    ('icims', 'globalcareers-sas')
    >>> board_key({"ats": "custom", "slug": None, "wd_tenant": None,
    ...            "careers_url": "https://x.com/careers/"})
    ('custom', 'https://x.com/careers')
    >>> board_key({"ats": None, "slug": None, "wd_tenant": None}) is None
    True
    """
    if r.get("ats") == "workday" and r.get("wd_tenant"):
        return ("workday", r["wd_tenant"], r.get("wd_pod"), r.get("wd_site"))
    if r.get("ats") in ("successfactors", "peopleadmin", "custom", "wpjson",
                        CAPTURE_ATS):
        u = (r.get("careers_url") or "").rstrip("/").lower()
        return (r["ats"], u) if u else None
    if r.get("ats") and r.get("slug"):
        return (r["ats"], r["slug"])
    return None


def company_by_board(conn, row):
    """The existing company row whose board matches `row`'s (see board_key),
    or None. Discovery resolves a pasted or harvested NAME to a board, and a
    name the roster spells differently ("SAS" vs "SAS Institute", "Veeva
    Systems" vs "Veeva", "NVIDIA AI" vs "NVIDIA" — all three re-added on
    2026-09-01) passes the name-keyed already-tracked check and lands as a
    second row on the same board until the next dedup. Checking the board
    before the insert stops the churn at the source."""
    key = board_key(row)
    if key is None:
        return None
    for c in conn.execute("SELECT * FROM companies").fetchall():
        c = dict(c)
        if board_key(c) == key:
            return c
    return None


def dedup_companies(conn):
    """Merge company rows that point at the SAME board (same ats+slug, or the
    same Workday triple) but were created under different name spellings
    ("IQVIA" vs "Quintiles IMS (IQVIA)") — the name-keyed upsert can't catch
    those, so the crawl fetches one board several times. Jobs are re-pointed to
    the kept row and tags merge, so the merge is lossless. Returns rows merged."""
    from collections import defaultdict

    rows = [dict(r) for r in conn.execute("SELECT * FROM companies")]
    jobcount = {cid: n for cid, n in conn.execute(
        "SELECT company_id, COUNT(*) FROM jobs GROUP BY company_id")}
    groups = defaultdict(list)
    for r in rows:
        k = board_key(r)
        if k:
            groups[k].append(r)

    def keep_rank(r):
        # Prefer a scored row, then active, then most-referenced, then the
        # shortest (most canonical) name.
        return (r.get("mission_tier") is not None, r.get("active") or 0,
                jobcount.get(r["id"], 0), -len(r.get("name") or ""))

    merged = 0
    for k, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=keep_rank, reverse=True)
        keep, losers = members[0], members[1:]
        tags = set(t for t in (keep.get("tags") or "").split(",") if t)
        for l in losers:
            tags |= set(t for t in (l.get("tags") or "").split(",") if t)
            # Re-point the loser's jobs AND rename them: jobs.company_name
            # is the denormalized display/grouping key (ranked_jobs groups
            # and the digest prints by it), so a merge that only moved
            # company_id left "Red Hat" and "Red Hat (IBM subsidiary, RTP
            # HQ)" as two companies in every ranking after the 2026-09-01
            # dedup, though both pointed at company 85.
            conn.execute("UPDATE jobs SET company_id=?, company_name=? "
                         "WHERE company_id=?",
                         (keep["id"], keep["name"], l["id"]))
            conn.execute("DELETE FROM companies WHERE id=?", (l["id"],))
        active = 1 if any(m.get("active") for m in members) else (keep.get("active") or 0)
        conn.execute("UPDATE companies SET tags=?, active=? WHERE id=?",
                     (",".join(sorted(tags)) or None, active, keep["id"]))
        merged += len(losers)
        print(f"    {keep['name'][:30]:30} <- merged {len(losers)}: "
              + ", ".join(l["name"][:20] for l in losers))
    # Realign every linked job with its company's current name — this
    # catches rows renamed by earlier (name-blind) merges and rows whose
    # ingest path spelled the company its own way ("BD (Becton Dickinson)"
    # linked to company "BD").
    realigned = conn.execute(
        "UPDATE jobs SET company_name=(SELECT name FROM companies "
        "WHERE companies.id=jobs.company_id) WHERE company_id IN "
        "(SELECT id FROM companies) AND company_name IS NOT "
        "(SELECT name FROM companies WHERE companies.id=jobs.company_id)"
    ).rowcount
    if realigned:
        print(f"    {realigned} job row(s) renamed to their company's name")
    conn.commit()
    return merged


def dedup_jobs(conn):
    """Collapse job rows that are the SAME posting under different ids: same
    company, same URL modulo scheme/query/fragment (_norm_url), same
    normalized title. upsert_job's re-key only catches an EXACT URL match,
    so a fetcher that emitted the same posting with a different query string
    (iCIMS `?in_iframe=1` vs `?hub=9&in_iframe=1`, 12 SAS pairs in the
    2026-09-01 store) under a second id namespace slipped past it, and the
    pair then double-ranked and double-spent deep-verify.

    Two guards keep this from eating distinct postings. Title must match —
    some custom boards give several DISTINCT postings one landing URL (see
    upsert_job). And the ids' per-posting tail (the board's own requisition
    number, "..._42453") must match too: Greenhouse companies whose stored
    URL is a shared careers landing page (butterflynetwork.com/careers?
    gh_jid=N) reduce to one URL for every job, and a title reposted under a
    fresh requisition (a second office, a re-opened req) is a separate
    posting, not a duplicate — the dry run without this guard would have
    merged three such Butterfly Network pairs.

    Keeps, per group: a dispositioned row over an undispositioned one, then
    an open row over a closed one, then the earliest first_seen (the row
    whose history is longest). Returns the number of rows deleted."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in conn.execute(
            "SELECT job_id, company_id, url, title, disposition, status, "
            "first_seen FROM jobs WHERE company_id IS NOT NULL "
            "AND url IS NOT NULL AND url != ''"):
        key = (r["company_id"], _norm_url(r["url"]), _norm_title(r["title"]),
               r["job_id"].rsplit("_", 1)[-1])
        if key[1] and key[2]:
            groups[key].append(dict(r))

    def keep_rank(r):
        return (r.get("disposition") is not None,
                (r.get("status") or "open") == "open",
                # earliest first: ISO strings sort by time, so negate via
                # tuple ordering by sorting descending on the inverse
                "" if not r.get("first_seen") else r["first_seen"])

    deleted = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        # Highest disposition/open rank wins; among equals the EARLIEST
        # first_seen (min) wins, so sort ascending on first_seen and
        # descending on the two flags.
        members.sort(key=lambda r: (not keep_rank(r)[0], not keep_rank(r)[1],
                                    keep_rank(r)[2]))
        keep, losers = members[0], members[1:]
        for l in losers:
            conn.execute("DELETE FROM jobs WHERE job_id=?", (l["job_id"],))
        deleted += len(losers)
        print(f"    {(keep['title'] or '')[:40]:40} kept {keep['job_id'][:28]}"
              f" <- dropped {', '.join(l['job_id'][:28] for l in losers)}")
    conn.commit()
    return deleted


def export_companies(conn, path):
    """Dump the company roster to JSON — the shareable/bootstrap artifact
    that replaced config.py's seed lists. Secrets-free by construction."""
    import json
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM companies ORDER BY name").fetchall()]
    for r in rows:
        r.pop("id", None)          # ids are per-database
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    return len(rows)


def import_companies(conn, path):
    """Upsert companies from an export_companies JSON file (idempotent;
    tags merge, existing mission scores survive None fields)."""
    import json
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    n = 0
    for r in rows:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        r.pop("id", None)
        upsert_company(conn, r)
        n += 1
    return n


def set_company_tag(conn, name, tag, add=True):
    """Add or remove one scope tag on a company (case-insensitive name
    match). Returns the company's new comma-joined tag string ('' when the
    last tag was removed), or None if no such company exists."""
    row = conn.execute(
        "SELECT id, tags FROM companies WHERE lower(name)=lower(?)",
        (name,)).fetchone()
    if not row:
        return None
    tags = {t for t in (row["tags"] or "").split(",") if t}
    (tags.add if add else tags.discard)(tag)
    val = ",".join(sorted(tags)) or None
    conn.execute("UPDATE companies SET tags=? WHERE id=?", (val, row["id"]))
    conn.commit()
    return val or ""


def get_company(conn, company_id):
    """One company row by id, or None."""
    if not company_id:
        return None
    row = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    return dict(row) if row else None


def company_id_by_name(conn, name):
    """Resolve a company name to its id (case-insensitive exact match), or
    None if the store has no such company. Used to link externally-ingested
    jobs to their vetted company row so they inherit its mission score."""
    if not name:
        return None
    row = conn.execute(
        "SELECT id FROM companies WHERE lower(name) = lower(?) LIMIT 1",
        (name,)).fetchone()
    return row["id"] if row else None


def get_companies(conn, active_only=True, missions=None, tag=None):
    """Companies, optionally filtered by mission tier(s) and/or scope tag."""
    q = "SELECT * FROM companies"
    conds, args = [], []
    if active_only:
        conds.append("active = 1")
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
    return [dict(r) for r in conn.execute(q, args).fetchall()]


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


def confirm_company(conn, cid):
    """Accept a review candidate onto the roster: the pending tag comes off
    and `active` follows the shared mission rule (core.claude.
    is_active_mission) applied to the tier already stored on the row.

    Returns the confirmed row, or None when there is no such company.

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, mark_pending(
    ...     {"name": "Acme", "ats": "lever", "slug": "acme", "tags": "local"}))
    >>> row = confirm_company(conn, company_id_by_name(conn, "Acme"))
    >>> row["tags"], row["active"]
    ('local', 1)

    The crawl picks it up from that moment; it could not see it before:

    >>> [c["name"] for c in crawlable_companies(conn)]
    ['Acme']

    >>> confirm_company(conn, 9999) is None
    True
    """
    from core.claude import is_active_mission
    row = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
    if not row:
        return None
    kept = tags.parse(row["tags"]) - {tags.PENDING}
    conn.execute(
        "UPDATE companies SET tags=?, active=? WHERE id=?",
        (tags.join(kept), is_active_mission(row["mission_tier"], row["name"]),
         cid))
    conn.commit()
    return get_company(conn, cid)


def reject_company(conn, cid, reason=None):
    """Throw a review candidate away for good: the row and any jobs it
    produced are deleted, and its name is blocklisted so no discovery path
    re-finds it.

    Returns the rejected name, or None when there is no such company.

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
#   * empty streak -- `dormant_after` consecutive DAYS returning nothing;
#   * off-mission volume -- >= 30 jobs stored and a best fit under 0.20.
# Watched companies are exempt from both: the watch tag means "tell me the
# moment anything opens here", which a weekly cadence would break.

# Off-mission volume rule. A board this big that has never scored above
# this is not a scoring accident, it is the wrong employer for the profile.
_OFFMISSION_MIN_JOBS = 30
_OFFMISSION_MAX_FIT = 0.20


def _offmission_volume(conn, company_id):
    """True when this company has stored >= 30 jobs and its BEST resume fit
    is still under 0.20 -- the high-volume off-mission board pattern. A NULL
    max (nothing scored yet) is missing data, not a verdict, so it fails."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(resume_fit_score) AS best FROM jobs "
        "WHERE company_id = ?", (company_id,)).fetchone()
    return bool(row and row["n"] >= _OFFMISSION_MIN_JOBS
                and row["best"] is not None
                and row["best"] < _OFFMISSION_MAX_FIT)


def record_crawl_outcome(conn, company_id, n_jobs, err=None,
                         dormant_after=4, dormant_days=7):
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
      * `n_jobs > 0` resets the streak and wakes a dormant row;
      * either dormancy rule (streak, off-mission volume) parks the row at
        now + `dormant_days`.

    Watched companies, and rows the user switched 'off', are left alone.
    """
    row = conn.execute(
        "SELECT id, tags, crawl_state, empty_streak, last_crawled_at "
        "FROM companies WHERE id = ?", (company_id,)).fetchone()
    if not row:
        return None
    state = row["crawl_state"] or "active"
    if err is not None:
        return state

    now = datetime.now()
    stamp = now.isoformat()
    streak = row["empty_streak"] or 0
    sets = {"last_crawled_at": stamp}

    if n_jobs:
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

    watched = "watch" in {t for t in (row["tags"] or "").split(",") if t}
    if state != "off" and not watched:
        if streak >= dormant_after or _offmission_volume(conn, company_id):
            state = "dormant"
            sets["next_crawl_at"] = (now + timedelta(days=dormant_days)
                                     ).isoformat()

    sets["crawl_state"] = state
    conn.execute(
        f"UPDATE companies SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
        [*sets.values(), company_id])
    conn.commit()
    return state


def _is_crawlable(company, now=None):
    """Does this company row come up for a crawl right now? A NULL
    crawl_state reads as 'active' (rows that predate the column), a dormant
    row only once its next_crawl_at has passed, an 'off' row never."""
    state = company.get("crawl_state") or "active"
    if state == "active":
        return True
    if state == "dormant":
        return (company.get("next_crawl_at") or "") <= (
            now or datetime.now().isoformat())
    return False


def crawlable_companies(conn, tag=None):
    """The active companies due for a crawl: everything except the dormant
    rows whose weekly slot has not come round yet. What build_sources and
    sync_status_all fetch, in place of every active row.

    Review candidates are `active = 0`, so they are never fetched -- the
    whole point of the queue is that an unconfirmed guess costs nothing:

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
    """
    now = datetime.now().isoformat()
    return [c for c in get_companies(conn, active_only=True, tag=tag)
            if c.get("ats") != CAPTURE_ATS and _is_crawlable(c, now)]


def reactivate_company(conn, company_id):
    """Undormant one company: back to 'active', streak cleared, no parked
    wake time. The escape hatch for a board the rules retired too eagerly."""
    conn.execute(
        "UPDATE companies SET crawl_state='active', empty_streak=0, "
        "next_crawl_at=NULL WHERE id=?", (company_id,))
    conn.commit()


# --------------------------------------------------------------------------- #
#  Jobs                                                                        #
# --------------------------------------------------------------------------- #

def job_exists(conn, job_id):
    return conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone() is not None


def upsert_job(conn, j):
    """Insert or refresh a job. Returns True if it was new.

    `first_seen` stays stable across re-runs; scores refresh so the stored
    values always reflect the latest scorer.
    """
    now = datetime.now().isoformat()
    new = not job_exists(conn, j["job_id"])
    if new and j.get("url"):
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
            (j["url"],)).fetchone()
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
             fit_model, posted_at, first_seen, last_seen, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             title=excluded.title, url=excluded.url, location=excluded.location,
             track=COALESCE(excluded.track, track),
             geo_mode=COALESCE(excluded.geo_mode, geo_mode),
             remote_eligible=COALESCE(excluded.remote_eligible, remote_eligible),
             remote_signal=COALESCE(excluded.remote_signal, remote_signal),
             anchor_signal=COALESCE(excluded.anchor_signal, anchor_signal),
             description=excluded.description,
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
                            THEN closed_at ELSE NULL END""",
        (j["job_id"], j.get("company_id"), j.get("company_name"), j.get("title"),
         j.get("url"), j.get("location"), track, j.get("geo_mode"),
         remote, j.get("remote_signal"), j.get("anchor_signal"),
         j.get("description"),
         j.get("resume_fit_score"), j.get("fit_reason"),
         j.get("fit_domain"), j.get("fit_function"), j.get("fit_stack"),
         j.get("fit_seniority"), j.get("fit_gates"), j.get("fit_model"),
         j.get("posted_at"), now, now, j.get("status", "open")),
    )
    conn.commit()
    return new


# --------------------------------------------------------------------------- #
#  Open/closed status                                                           #
# --------------------------------------------------------------------------- #

def _norm_title(t):
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


def _norm_url(u):
    """Scheme/query/fragment/trailing-slash-insensitive URL key."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    return u.split("#", 1)[0].split("?", 1)[0].rstrip("/")


def set_job_status(conn, job_id, status):
    """Mark one job 'open' or 'closed' directly (closed_at maintained)."""
    conn.execute(
        "UPDATE jobs SET status=?, closed_at=? WHERE job_id=?",
        (status, datetime.now().isoformat() if status == "closed" else None,
         job_id))
    conn.commit()


def resolve_job(conn, ref):
    """Resolve a user-supplied job reference to rows: exact job_id first,
    then unique job_id substring, then normalized URL. Returns a list of
    matching rows (ideally one; several = ambiguous; empty = no match) so
    callers can report ambiguity instead of guessing."""
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (ref,)).fetchone()
    if row:
        return [dict(row)]
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE job_id LIKE ?", (f"%{ref}%",)).fetchall()]
    if rows:
        return rows
    want = _norm_url(ref)
    if want:
        return [dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()
                if _norm_url(r["url"]) == want]
    return []


def set_disposition(conn, ref, disposition, note=None):
    """Record the user's decision on one job. `ref` is a job_id, a unique
    job_id fragment, or the posting URL; `disposition` is one of
    DISPOSITIONS, or 'none'/'clear' to erase. Returns (row, error) — row is
    the matched job on success, error a printable message otherwise.

    Marking a row 'applied' also stamps `applied_at`, once: a later
    interviewing/rejected leaves the original apply date alone. See
    tests/test_store.py::TestPipelineTracking."""
    d = (disposition or "").strip().lower()
    clearing = d in ("none", "clear")
    if not clearing and d not in DISPOSITIONS:
        return None, (f"unknown disposition {disposition!r} — use one of "
                      f"{', '.join(DISPOSITIONS)} (or 'clear')")
    matches = resolve_job(conn, ref)
    if not matches:
        return None, f"no job matches {ref!r} (job_id, id fragment, or URL)"
    if len(matches) > 1:
        opts = "\n".join(f"    {m['job_id']}  {(m['title'] or '')[:50]}"
                         for m in matches[:8])
        return None, f"{ref!r} is ambiguous ({len(matches)} matches):\n{opts}"
    row = matches[0]
    now = datetime.now().isoformat()
    sets = ["disposition=?", "disposition_note=?", "disposition_at=?"]
    args = [None if clearing else d, None if clearing else note,
            None if clearing else now]
    if d == "applied":
        # COALESCE, not an assignment: the FIRST apply owns the date. Without
        # it, re-marking a row that came back 'rejected' and then 'applied'
        # again — or any later edit — would silently reset the clock every
        # elapsed-time question is measured against.
        sets.append("applied_at=COALESCE(applied_at, ?)")
        args.append(now)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?",
                 [*args, row["job_id"]])
    conn.commit()
    return row, None


def get_pipeline(conn):
    """Every job the user has dispositioned, newest decision first — the
    digest's pipeline section and the --pipeline CLI. Includes closed rows
    on purpose: 'posting closed after you applied' is a signal."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE disposition IS NOT NULL "
        "ORDER BY disposition_at DESC").fetchall()]


def update_pipeline_fields(conn, job_id, **fields):
    """Write the application-tracking columns (PIPELINE_FIELDS) on one job.

    Any other column name is refused rather than written, empty strings
    normalize to NULL, `referral` to 0/1, and `outcome_reason` must be one of
    OUTCOME_REASONS. Returns (row, error) like set_disposition — the updated
    job on success, a printable message otherwise. See
    tests/test_store.py::TestPipelineTracking.

    Notes:
        The whitelist is the point: this is reachable from the browser, and a
        blanket "UPDATE jobs SET <whatever the JSON body named>" would let a
        typo — or a crafted request — overwrite a scorer-owned column such as
        resume_fit_score or the validated `disposition` itself.
    """
    unknown = sorted(set(fields) - set(PIPELINE_FIELDS))
    if unknown:
        return None, (f"unknown pipeline field(s) {', '.join(unknown)} — "
                      f"writable: {', '.join(PIPELINE_FIELDS)}")
    if conn.execute("SELECT 1 FROM jobs WHERE job_id=?",
                    (job_id,)).fetchone() is None:
        return None, f"no job matches {job_id!r}"
    sets = {}
    for k, v in fields.items():
        if k == "referral":
            sets[k] = None if v is None else int(bool(v))
            continue
        v = (str(v).strip() if v is not None else "") or None
        if k == "outcome_reason" and v is not None and v not in OUTCOME_REASONS:
            return None, (f"unknown outcome_reason {v!r} — use one of "
                          f"{', '.join(OUTCOME_REASONS)}")
        sets[k] = v
    if sets:
        conn.execute(
            f"UPDATE jobs SET {', '.join(f'{k}=?' for k in sets)} "
            f"WHERE job_id=?", [*sets.values(), job_id])
        conn.commit()
    return dict(conn.execute("SELECT * FROM jobs WHERE job_id=?",
                             (job_id,)).fetchone()), None


def _fit_band(score):
    """The conversion_report bucket one resume_fit_score falls in.

    >>> _fit_band(0.2), _fit_band(0.45), _fit_band(0.9)
    ('low', 'mid', 'high')

    Each edge belongs to the band above it, and a perfect score still lands
    in the top band rather than off the end:

    >>> _fit_band(0.4), _fit_band(0.6), _fit_band(1.0)
    ('mid', 'high', 'high')

    A row the scorer never reached is reported separately, not counted as a
    weak one:

    >>> _fit_band(None)
    'unscored'
    """
    if score is None:
        return "unscored"
    for name, lo, hi in FIT_BANDS:
        if lo <= score < hi:
            return name
    return FIT_BANDS[-1][0] if score >= FIT_BANDS[-1][1] else FIT_BANDS[0][0]


def conversion_report(conn):
    """Where applications actually convert, sliced by fit band and geo_mode.

    One dict per (band, geo_mode) that has at least one application, ordered
    by band (FIT_BANDS low to high, then 'unscored') then geo_mode. Each
    carries `applications` (every row that went out), the live `applied` and
    `interviewing` counts, `rejected`, `interviews`, and `interview_rate` =
    interviews / applications, rounded to three places.

    `interviews` counts a row that REACHED an interview, which is not the
    same as one sitting in 'interviewing': a rejected row whose
    outcome_reason is 'rejected-interview' got there too, and without it
    every conversion number would decay as applications resolve. See
    tests/test_store.py::TestPipelineTracking.
    """
    ph = ",".join("?" for _ in APPLIED_DISPOSITIONS)
    rows = conn.execute(
        f"SELECT resume_fit_score, geo_mode, disposition, outcome_reason "
        f"FROM jobs WHERE disposition IN ({ph})",
        APPLIED_DISPOSITIONS).fetchall()
    order = [name for name, _, _ in FIT_BANDS] + ["unscored"]
    buckets = {}
    for r in rows:
        key = (_fit_band(r["resume_fit_score"]), r["geo_mode"] or "unknown")
        bucket = buckets.setdefault(key, {
            "band": key[0], "geo_mode": key[1], "applications": 0,
            "applied": 0, "interviewing": 0, "rejected": 0, "interviews": 0})
        bucket["applications"] += 1
        bucket[r["disposition"]] += 1
        if (r["disposition"] == "interviewing"
                or r["outcome_reason"] == "rejected-interview"):
            bucket["interviews"] += 1
    out = sorted(buckets.values(),
                 key=lambda x: (order.index(x["band"]), x["geo_mode"]))
    for bucket in out:
        bucket["interview_rate"] = round(
            bucket["interviews"] / bucket["applications"], 3)
    return out


def followups_due(conn, today=None):
    """Live applications whose follow-up date has arrived, oldest first.

    A row qualifies when `followup_at` is set and not in the future and the
    application is still live (LIVE_DISPOSITIONS) — nudging a rejected row
    is noise. `today` is a 'YYYY-MM-DD' string and defaults to today. See
    tests/test_store.py::TestPipelineTracking.
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    ph = ",".join("?" for _ in LIVE_DISPOSITIONS)
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM jobs WHERE COALESCE(followup_at,'') != '' "
        f"AND followup_at <= ? AND disposition IN ({ph}) "
        f"ORDER BY followup_at", (today, *LIVE_DISPOSITIONS)).fetchall()]


def touch_job(conn, job_id):
    """Record that a job was just observed live at its source — reopen it and
    refresh last_seen, touching nothing else. For dedupe paths that skip the
    full upsert (e.g. a re-captured LinkedIn card already in the store): the
    sighting must still reset the closed flag and the external-row grace
    clock (see sync_job_statuses), or the next board sync could re-close a
    posting the user just saw live."""
    conn.execute(
        "UPDATE jobs SET status='open', closed_at=NULL, last_seen=? "
        "WHERE job_id=?", (datetime.now().isoformat(), job_id))
    conn.commit()


def sync_job_statuses(conn, company_id, fetched_jobs, track=None,
                      external_grace_days=3):
    """Reconcile ONE company's stored jobs against a live board snapshot
    (`fetched_jobs`: dicts with id/title/url, as returned by
    fetchers.company.fetch_company). Rows matched by job_id, URL, or
    normalized title are (re)marked open and their last_seen touched; rows
    that have vanished from the snapshot are marked closed. Returns
    (n_reopened, n_closed).

    Caller contract: only pass a snapshot from a SUCCESSFUL, non-empty fetch
    — fetchers soft-fail to [] (HTTP 404, non-JSON), which is
    indistinguishable from a genuinely emptied board, so an empty snapshot
    must never close anything (this function no-ops on one).

    Matching depends on where the row's job_id came from:
      * BOARD-NATIVE rows — job_id in the snapshot's own id namespace (same
        "<ats>_<key>_" prefix as some snapshot id) — match by EXACT id only.
        The board is authoritative for its own ids, and boards recycle
        titles across requisitions (Beacon reposts "Algorithm Engineer"
        under a fresh Greenhouse id every cycle), so a title/URL fallback
        would let one live posting shield every dead same-titled req from
        ever closing. Absent id -> closed immediately. A partial fetch
        (e.g. Workday pagination dying mid-board) can close rows
        spuriously, but the next full fetch reopens them (matched rows
        always flip back).
      * EXTERNAL rows (LinkedIn captures, NLx, manual --add, legacy ids
        from a retired fetcher) can never id-match, so they match by
        normalized URL or title instead, and are closed only after
        `external_grace_days` without being seen — a manual --add isn't
        insta-closed just because its title doesn't exactly match a board
        row.
      * When `track` is given, only rows of that track are ever CLOSED
        (matched rows are reopened regardless — they're live on the board).
    """
    if not company_id or not fetched_jobs:
        return (0, 0)
    ids = {j.get("id") for j in fetched_jobs if j.get("id")}
    urls = {u for u in (_norm_url(j.get("url")) for j in fetched_jobs) if u}
    titles = {t for t in (_norm_title(j.get("title")) for j in fetched_jobs) if t}
    # Posting dates piggyback on the sync: every matched row gets its NULL
    # posted_at backfilled from the live snapshot, so the whole store gains
    # real posting dates over normal crawls with zero extra HTTP.
    posted = {}
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
    prefixes = {"_".join(i.split("_", 2)[:2]) + "_" for i in ids if "_" in i}
    now = datetime.now().isoformat()
    grace_cutoff = (datetime.now()
                    - timedelta(days=external_grace_days)).isoformat()
    n_reopened = n_closed = 0
    rows = conn.execute(
        "SELECT job_id, url, title, track, status, first_seen, last_seen "
        "FROM jobs WHERE company_id=?", (company_id,)).fetchall()
    for r in rows:
        board_native = any(r["job_id"].startswith(p) for p in prefixes)
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
                "posted_at=COALESCE(posted_at, ?) WHERE job_id=?",
                (now, p, r["job_id"]))
            continue
        if track is not None and track not in track_set(r["track"]):
            continue
        if (r["status"] or "open") == "closed":
            continue
        seen = r["last_seen"] or r["first_seen"] or ""
        if board_native or seen < grace_cutoff:   # ISO strings sort by time
            conn.execute(
                "UPDATE jobs SET status='closed', closed_at=? WHERE job_id=?",
                (now, r["job_id"]))
            n_closed += 1
    conn.commit()
    return (n_reopened, n_closed)


# Fit columns written together by the rescore path (see update_job_scores).
_SCORE_COLS = ("resume_fit_score", "fit_reason", "fit_gates", "fit_model",
               "fit_domain", "fit_function", "fit_stack", "fit_seniority")


def update_job_scores(conn, job_id, cols):
    """Overwrite only the fit columns for one job (used by rescore). `cols` is a
    FitResult.as_columns() dict; any missing key is written NULL, so passing an
    empty/partial dict clears a stale score (an unscorable row drops out of
    ranking)."""
    sets = ", ".join(f"{c}=?" for c in _SCORE_COLS)
    conn.execute(f"UPDATE jobs SET {sets} WHERE job_id=?",
                 [cols.get(c) for c in _SCORE_COLS] + [job_id])
    conn.commit()


# Matches the fit_reason tag summary() writes: "[dom0.45 fun0.72 sta0.55
# sen0.80 gate:geo+embedded] reason". Gates are '+'-joined in the tag.
_AXIS_TAG = re.compile(
    r"\[dom([\d.]+) fun([\d.]+) sta([\d.]+) sen([\d.]+)(?: gate:([^\]]+))?\]")


def backfill_axis_columns(conn):
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
        m = _AXIS_TAG.match(r["fit_reason"] or "")
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
    conn.commit()
    print(f"  {n} of {len(rows)} row(s) backfilled from fit_reason tags.")
    return n


def remote_admitted(row, remote_mission_floor):
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


def ranked_jobs(conn, track=None, limit=None, location_re=None, rank_by="combined",
                allow_geo_modes=None, min_mission=None,
                remote_mission_floor=None, include_closed=False,
                include_dispositioned=False):
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
    stays visible."""
    q = """
      SELECT j.*, c.mission_tier, c.mission_score, c.tags AS company_tags
      FROM jobs j LEFT JOIN companies c ON j.company_id = c.id
    """
    conds, args = [], []
    if track:
        conds.append(_TRACK_MATCH_SQL)
        args.append(_track_match_arg(track))
    if not include_closed:
        conds.append("COALESCE(j.status,'open') != 'closed'")
    if not include_dispositioned:
        ph = ",".join("?" for _ in RANKING_EXCLUDED_DISPOSITIONS)
        conds.append(f"(j.disposition IS NULL OR j.disposition NOT IN ({ph}))")
        args += list(RANKING_EXCLUDED_DISPOSITIONS)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    if location_re is not None:
        rows = [r for r in rows
                if location_re.search(r.get("location") or "")
                or (allow_geo_modes and r.get("geo_mode") in allow_geo_modes
                    and remote_admitted(r, remote_mission_floor))]
    def _effective_mission(r):
        # A conglomerate's own mission score is ~0.05 (off-mission overall),
        # but a job here already passed the health keyword filter at crawl
        # time — so rank it at the keyword-vetted floor, not the company's
        # score, or its combined rank would be sunk unfairly.
        mission = r.get("mission_score")
        if config.is_multi_division(r.get("company_name")):
            mission = max(mission or 0.0, config.MULTI_DIVISION_MISSION_FLOOR)
        return mission

    for r in rows:
        r["combined_score"] = combined_score(r.get("resume_fit_score"),
                                             _effective_mission(r))
    if min_mission is not None:
        rows = [r for r in rows
                if (m := _effective_mission(r)) is None or m >= min_mission]
    # Primary sort key per rank_by, then the other factors as tiebreaks; None
    # sorts last via the -1 sentinel (all real scores are >= 0).
    primary = "resume_fit_score" if rank_by == "fit" else "combined_score"
    def _k(r):
        vals = (r.get(primary), r.get("combined_score"),
                r.get("resume_fit_score"), r.get("mission_score"))
        return tuple(v if v is not None else -1.0 for v in vals)
    rows.sort(key=_k, reverse=True)
    if limit:
        rows = rows[:int(limit)]
    return rows


# --------------------------------------------------------------------------- #
#  Seen-jobs compatibility (dedupe-only callers)                               #
# --------------------------------------------------------------------------- #

def is_new(conn, job_id):
    """Dedupe check against the unified jobs table."""
    return not job_exists(conn, job_id)


def mark_seen(conn, job, track=None):
    """Record a fetched job dict ({id, company, title, url, location, ...})
    in the unified jobs table. Adapter for callers that only need
    seen/unseen dedupe semantics.

    Fit columns are passed through when the caller has already scored the
    job in place (e.g. remote_neural_run's ``--fit --commit`` path, which
    ``j.update(FitResult.as_columns())``s before committing). Dedupe-only
    callers simply omit those keys, so ``.get`` yields None and upsert_job's
    COALESCE preserves any existing score — this adapter never clobbers a
    stored score with a null. Without this pass-through, a ``--fit --commit``
    run computed scores, wrote them to the digest, and then dropped every
    one on the DB write."""
    upsert_job(conn, {
        "job_id":          job["id"],
        "company_name":    job.get("company"),
        "title":           job.get("title"),
        "url":             job.get("url"),
        "location":        job.get("location"),
        "track":           track or job.get("track"),
        "remote_eligible": job.get("remote_eligible"),
        "remote_signal":   job.get("remote_signal"),
        "anchor_signal":   job.get("anchor_signal"),
        "description":     (job.get("description") or "")[:config.MAX_DESC_CHARS],
        "posted_at":       job.get("posted_at"),
        "resume_fit_score": job.get("resume_fit_score"),
        "fit_reason":      job.get("fit_reason"),
        "fit_gates":       job.get("fit_gates"),
        "fit_domain":      job.get("fit_domain"),
        "fit_function":    job.get("fit_function"),
        "fit_stack":       job.get("fit_stack"),
        "fit_seniority":   job.get("fit_seniority"),
        "fit_model":       job.get("fit_model"),
    })
