"""
Unified SQLite store shared by every track: a `companies` table (with a
cached mission score and scope tags) and a `jobs` table (per-job scores,
dedup state, and per-track fields).

Design (merged from both development tracks):
  * The company row carries the mission judgment once, so individual jobs
    inherit it instead of paying a per-job mission LLM call — "the company
    list simplifies the job list."  (local-clinical insight)
"""

import sqlite3
from datetime import datetime

import config


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
    hq_location    TEXT,
    nc_job_count   INTEGER DEFAULT 0,
    total_job_count INTEGER DEFAULT 0,
    mission_tier   TEXT,              -- healthcare-tech|health-bio-science|other
    mission_score  REAL,              -- 0..1 (health/tech relevance)
    mission_reason TEXT,
    tags           TEXT,              -- comma tokens: neural,nc_local,remote_friendly
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
    track          TEXT,                  -- remote-neural|local-tech|classic
    geo_mode       TEXT,                  -- onsite|remote
    remote_eligible INTEGER,              -- 1 when the remote filter passed
    remote_signal  TEXT,                  -- phrase/hint that marked it remote
    neural_signal  TEXT,                  -- neural anchor term that matched
    mission        TEXT,                  -- job-level mission tier (if scored)
    description    TEXT,
    tech_bar_score REAL,
    resume_fit_score REAL,
    fit_reason     TEXT,
    first_seen     TEXT,
    last_seen      TEXT,
    status         TEXT DEFAULT 'open'
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
    },
    "jobs": {
        "track":           "TEXT",
        "remote_eligible": "INTEGER",
        "remote_signal":   "TEXT",
        "neural_signal":   "TEXT",
        "mission":         "TEXT",
    },
}


def _ensure_columns(conn):
    for table, cols in _MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def connect(path=None):
    conn = sqlite3.connect(path or config.STORE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    _ensure_columns(conn)
    conn.executescript(_INDEXES)
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
#  Companies                                                                   #
# --------------------------------------------------------------------------- #

_COMPANY_COLS = (
    "name", "ats", "slug", "wd_tenant", "wd_pod", "wd_site", "careers_url",
    "hq_location", "nc_job_count", "total_job_count", "mission_tier",
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
    q += " ORDER BY mission_score DESC, nc_job_count DESC"
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
    conn.execute(
        """INSERT INTO jobs
            (job_id, company_id, company_name, title, url, location, track,
             geo_mode, remote_eligible, remote_signal, neural_signal, mission,
             description, tech_bar_score, resume_fit_score, fit_reason,
             first_seen, last_seen, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             title=excluded.title, url=excluded.url, location=excluded.location,
             track=COALESCE(excluded.track, track),
             geo_mode=COALESCE(excluded.geo_mode, geo_mode),
             remote_eligible=COALESCE(excluded.remote_eligible, remote_eligible),
             remote_signal=COALESCE(excluded.remote_signal, remote_signal),
             neural_signal=COALESCE(excluded.neural_signal, neural_signal),
             mission=COALESCE(excluded.mission, mission),
             description=excluded.description,
             tech_bar_score=COALESCE(excluded.tech_bar_score, tech_bar_score),
             resume_fit_score=COALESCE(excluded.resume_fit_score, resume_fit_score),
             fit_reason=COALESCE(NULLIF(excluded.fit_reason,''), fit_reason),
             last_seen=excluded.last_seen,
             status=excluded.status""",
        (j["job_id"], j.get("company_id"), j.get("company_name"), j.get("title"),
         j.get("url"), j.get("location"), j.get("track"), j.get("geo_mode"),
         remote, j.get("remote_signal"), j.get("neural_signal"),
         j.get("mission"), j.get("description"), j.get("tech_bar_score"),
         j.get("resume_fit_score"), j.get("fit_reason"),
         now, now, j.get("status", "open")),
    )
    conn.commit()
    return new


def ranked_jobs(conn, track=None, limit=None):
    """Jobs joined to company mission, ranked by resume fit then mission."""
    q = """
      SELECT j.*, c.mission_tier, c.mission_score, c.hq_location AS company_location
      FROM jobs j LEFT JOIN companies c ON j.company_id = c.id
    """
    args = []
    if track:
        q += " WHERE j.track = ?"
        args.append(track)
    q += " ORDER BY j.resume_fit_score DESC NULLS LAST, c.mission_score DESC NULLS LAST"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


# --------------------------------------------------------------------------- #
#  Seen-jobs compatibility (dedupe-only callers)                               #
# --------------------------------------------------------------------------- #

def is_new(conn, job_id):
    """Dedupe check against the unified jobs table."""
    return not job_exists(conn, job_id)


def mark_seen(conn, job, track=None):
    """Record a fetched job dict ({id, company, title, url, location, ...})
    in the unified jobs table. Adapter for callers that only need
    seen/unseen dedupe semantics."""
    upsert_job(conn, {
        "job_id":          job["id"],
        "company_name":    job.get("company"),
        "title":           job.get("title"),
        "url":             job.get("url"),
        "location":        job.get("location"),
        "track":           track or job.get("track"),
        "remote_eligible": job.get("remote_eligible"),
        "remote_signal":   job.get("remote_signal"),
        "neural_signal":   job.get("neural_signal"),
        "description":     (job.get("description") or "")[:2000],
    })
