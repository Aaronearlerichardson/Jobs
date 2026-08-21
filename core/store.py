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
    notes          TEXT
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
    resume_fit_score REAL,
    fit_reason     TEXT,
    first_seen     TEXT,
    last_seen      TEXT,
    status         TEXT DEFAULT 'open'   -- open|closed (see sync_job_statuses)
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
    },
}

# The user's recorded decision on a job. `saved` = shortlisted, still shown
# in ranking; the rest leave the ranking: applied/interviewing move to the
# digest's pipeline section, rejected/dismissed disappear (and dismissed
# rows become negative few-shot examples for the fit scorer — 
# fit.py reads them, so a --note saying WHY is worth writing).
DISPOSITIONS = ("saved", "applied", "interviewing", "rejected", "dismissed")
RANKING_EXCLUDED_DISPOSITIONS = ("applied", "interviewing", "rejected", "dismissed")

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
    "last_probed", "notes",
)


def upsert_company(conn, c):
    """Insert or update a company by name. `c` is a dict of column->value.

    `tags` merge instead of overwrite: a company discovered by the local
    sourcing pass ("nc_local") and later by BCI discovery ("neural") keeps
    both scopes.
    """
    c = {**c, "last_probed": c.get("last_probed") or datetime.now().isoformat()}
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
    updates = ", ".join(f"{k}=excluded.{k}" for k in cols if k != "name")
    conn.execute(
        f"INSERT INTO companies ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(name) DO UPDATE SET {updates}",
        [c[k] for k in cols],
    )
    conn.commit()
    row = conn.execute("SELECT id FROM companies WHERE name=?", (c["name"],)).fetchone()
    return row["id"] if row else None


def prune_dead_boards(conn, max_workers=12, deactivate_offmission=False):
    """Deactivate active companies whose JSON-API ATS board no longer resolves
    (a hard 404/error — the source of the crawl's `HTTP 404` spam), and
    optionally off-mission `other`-tier companies (excluding multi-division).
    Only greenhouse/lever/ashby/bamboohr are probed — their board endpoint
    cleanly distinguishes "exists" (200) from "dead" (404). Returns
    (n_dead, n_offmission)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import config
    from discovery.probes import (probe_greenhouse, probe_lever,
                                   probe_ashby, probe_bamboohr)
    PROBE = {"greenhouse": probe_greenhouse, "lever": probe_lever,
             "ashby": probe_ashby, "bamboohr": probe_bamboohr}

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


def dedup_companies(conn):
    """Merge company rows that point at the SAME board (same ats+slug, or the
    same Workday triple) but were created under different name spellings
    ("IQVIA" vs "Quintiles IMS (IQVIA)") — the name-keyed upsert can't catch
    those, so the crawl fetches one board several times. Jobs are re-pointed to
    the kept row and tags merge, so the merge is lossless. Returns rows merged."""
    from collections import defaultdict

    def board_key(r):
        if r["ats"] == "workday" and r["wd_tenant"]:
            return ("workday", r["wd_tenant"], r["wd_pod"], r["wd_site"])
        # careers_url-keyed ATSes: their slug is a shared datacenter host
        # (SuccessFactors "performancemanagerN" serves many tenants) or
        # absent, and the careers_url IS the board identity. Keying these on
        # slug merged Bayer into Sonova (both performancemanager5).
        if r["ats"] in ("successfactors", "peopleadmin", "custom", "wpjson"):
            u = (r.get("careers_url") or "").rstrip("/").lower()
            return (r["ats"], u) if u else None
        if r["ats"] and r["slug"]:
            return (r["ats"], r["slug"])
        return None

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
            conn.execute("UPDATE jobs SET company_id=? WHERE company_id=?",
                         (keep["id"], l["id"]))
            conn.execute("DELETE FROM companies WHERE id=?", (l["id"],))
        active = 1 if any(m.get("active") for m in members) else (keep.get("active") or 0)
        conn.execute("UPDATE companies SET tags=?, active=? WHERE id=?",
                     (",".join(sorted(tags)) or None, active, keep["id"]))
        merged += len(losers)
        print(f"    {keep['name'][:30]:30} <- merged {len(losers)}: "
              + ", ".join(l["name"][:20] for l in losers))
    conn.commit()
    return merged


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
             posted_at, first_seen, last_seen, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
         j.get("fit_seniority"), j.get("fit_gates"),
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
    the matched job on success, error a printable message otherwise."""
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
    conn.execute(
        "UPDATE jobs SET disposition=?, disposition_note=?, disposition_at=? "
        "WHERE job_id=?",
        (None if clearing else d, None if clearing else note,
         None if clearing else datetime.now().isoformat(), row["job_id"]))
    conn.commit()
    return row, None


def get_pipeline(conn):
    """Every job the user has dispositioned, newest decision first — the
    digest's pipeline section and the --pipeline CLI. Includes closed rows
    on purpose: 'posting closed after you applied' is a signal."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE disposition IS NOT NULL "
        "ORDER BY disposition_at DESC").fetchall()]


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
_SCORE_COLS = ("resume_fit_score", "fit_reason", "fit_gates",
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


def ranked_jobs(conn, track=None, limit=None, location_re=None, rank_by="combined",
                allow_geo_modes=None, min_mission=None, include_closed=False,
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
    already qualifies them — ONLY at companies carrying the 'watch' tag.
    Watch is the one human-curated tag ("show me everything at this
    employer"), so it can be trusted with an out-of-area exception; the
    machine-set 'neural' tag cannot — auto-probed boards include slug
    collisions (an EEG company's row that actually points at a global AI
    board), and an unscoped geo_mode exception let 87 remote-anywhere rows
    into a 534-row local ranking.

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
    def _watched(r):
        tags = {t.strip() for t in (r.get("company_tags") or "").split(",")}
        return "watch" in tags

    if location_re is not None:
        rows = [r for r in rows
                if location_re.search(r.get("location") or "")
                or (allow_geo_modes and r.get("geo_mode") in allow_geo_modes
                    and _watched(r))]
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
    })
