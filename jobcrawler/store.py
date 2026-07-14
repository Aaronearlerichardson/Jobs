"""
Unified SQLite store shared by every track: a `companies` table (with a
cached mission score and scope tags) and a `jobs` table (per-job scores,
dedup state, and per-track fields).

Design (merged from both development tracks):
  * The company row carries the mission judgment once, so individual jobs
    inherit it instead of paying a per-job mission LLM call — "the company
    list simplifies the job list."  (local-clinical insight)
"""

import math
import sqlite3
from datetime import datetime

import config


def combined_score(fit, mission):
    """Geometric mean sqrt(fit * mission) of the resume-fit and company
    mission scores (both 0..1). Returns None if either is missing, so a job
    is only ranked once both factors are known. The geometric mean punishes
    imbalance: a strong fit at a weak-mission company scores far below a job
    that is solid on both axes (0.9*0.2 -> 0.42 < balanced 0.5*0.5 -> 0.50)."""
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
    nc_job_count   INTEGER DEFAULT 0,
    total_job_count INTEGER DEFAULT 0,
    mission_tier   TEXT,              -- healthcare-tech|health-bio-science|community-driven-tech|other
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
    description    TEXT,
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
    },
}

# Columns retired after the unified refactor. Dropped idempotently on connect
# so existing DBs (which keep old columns under CREATE TABLE IF NOT EXISTS)
# shed them too. All three were 100% NULL — mission/tech_bar_score became
# company-level after unification; hq_location was never populated.
_DROPPED_COLUMNS = {
    "jobs": ("mission", "tech_bar_score"),
    "companies": ("hq_location",),
}


def _ensure_columns(conn):
    for table, cols in _MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    for table, cols in _DROPPED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col in existing:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
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
    "nc_job_count", "total_job_count", "mission_tier",
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
    from .discovery.probes import (probe_greenhouse, probe_lever,
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
        off = [c for c in get_companies(conn, active_only=True)
               if c.get("mission_tier") == "other"
               and not config.is_multi_division(c.get("name"))]
        for c in off:
            conn.execute("UPDATE companies SET active=0 WHERE id=?", (c["id"],))
            print(f"    [other] {c['name'][:30]:30} {c['ats'] or '?':10} "
                  f"mission_score={c.get('mission_score')}")
        n_off = len(off)
    conn.commit()
    return len(dead), n_off


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
             geo_mode, remote_eligible, remote_signal, neural_signal,
             description, resume_fit_score, fit_reason,
             first_seen, last_seen, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             title=excluded.title, url=excluded.url, location=excluded.location,
             track=COALESCE(excluded.track, track),
             geo_mode=COALESCE(excluded.geo_mode, geo_mode),
             remote_eligible=COALESCE(excluded.remote_eligible, remote_eligible),
             remote_signal=COALESCE(excluded.remote_signal, remote_signal),
             neural_signal=COALESCE(excluded.neural_signal, neural_signal),
             description=excluded.description,
             resume_fit_score=COALESCE(excluded.resume_fit_score, resume_fit_score),
             fit_reason=COALESCE(NULLIF(excluded.fit_reason,''), fit_reason),
             last_seen=excluded.last_seen,
             status=excluded.status""",
        (j["job_id"], j.get("company_id"), j.get("company_name"), j.get("title"),
         j.get("url"), j.get("location"), j.get("track"), j.get("geo_mode"),
         remote, j.get("remote_signal"), j.get("neural_signal"),
         j.get("description"),
         j.get("resume_fit_score"), j.get("fit_reason"),
         now, now, j.get("status", "open")),
    )
    conn.commit()
    return new


def ranked_jobs(conn, track=None, limit=None, location_re=None):
    """Jobs joined to company mission, ranked by the combined score
    sqrt(resume_fit * company_mission). Jobs missing either factor fall to
    the bottom (combined is None), ordered among themselves by whatever
    score they do have.

    `location_re` (a compiled regex) enforces geography at query time,
    independent of the `track` label: a job whose stored location doesn't
    match is excluded from this search but stays in the shared table. This
    is how the local track keeps out-of-area postings out of its results no
    matter which ingest path stamped them `local-tech`."""
    q = """
      SELECT j.*, c.mission_tier, c.mission_score
      FROM jobs j LEFT JOIN companies c ON j.company_id = c.id
    """
    args = []
    if track:
        q += " WHERE j.track = ?"
        args.append(track)
    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    if location_re is not None:
        rows = [r for r in rows if location_re.search(r.get("location") or "")]
    for r in rows:
        # A conglomerate's own mission score is ~0.05 (off-mission overall),
        # but a job here already passed the health keyword filter at crawl
        # time — so rank it at the keyword-vetted floor, not the company's
        # score, or its combined rank would be sunk unfairly.
        mission = r.get("mission_score")
        if config.is_multi_division(r.get("company_name")):
            mission = max(mission or 0.0, config.MULTI_DIVISION_MISSION_FLOOR)
        r["combined_score"] = combined_score(r.get("resume_fit_score"), mission)
    # Sort by combined desc, then the individual factors as tiebreaks; None
    # sorts last via the -1 sentinel (all real scores are >= 0).
    rows.sort(key=lambda r: (r["combined_score"] if r["combined_score"] is not None else -1.0,
                             r["resume_fit_score"] if r["resume_fit_score"] is not None else -1.0,
                             r["mission_score"] if r["mission_score"] is not None else -1.0),
              reverse=True)
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
