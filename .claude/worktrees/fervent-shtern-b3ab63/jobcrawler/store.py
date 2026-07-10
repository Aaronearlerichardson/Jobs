"""
SQLite store for the LOCAL-TECH crawler: a `companies` table (with a cached
mission score) and a `jobs` table (with a per-job résumé-fit score).

The company row carries the mission judgment once, so individual jobs inherit
it instead of paying a per-job mission LLM call — "the company list simplifies
the job list". Jobs still get their own résumé-fit score.
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
    slug           TEXT,              -- gh/lever/ashby board slug
    wd_tenant      TEXT,              -- workday triple
    wd_pod         INTEGER,
    wd_site        TEXT,
    careers_url    TEXT,
    hq_location    TEXT,
    nc_job_count   INTEGER DEFAULT 0,
    total_job_count INTEGER DEFAULT 0,
    mission_tier   TEXT,              -- healthcare-tech|health-bio-science|other
    mission_score  REAL,             -- 0..1 (health/tech relevance)
    mission_reason TEXT,
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
    geo_mode       TEXT,                  -- onsite|remote
    description    TEXT,
    tech_bar_score REAL,
    resume_fit_score REAL,
    fit_reason     TEXT,
    first_seen     TEXT,
    last_seen      TEXT,
    status         TEXT DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS ix_jobs_company ON jobs(company_id);
"""


def connect(path=None):
    conn = sqlite3.connect(path or config.LOCAL_TECH_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
#  Companies                                                                   #
# --------------------------------------------------------------------------- #

_COMPANY_COLS = (
    "name", "ats", "slug", "wd_tenant", "wd_pod", "wd_site", "careers_url",
    "hq_location", "nc_job_count", "total_job_count", "mission_tier",
    "mission_score", "mission_reason", "source", "active", "last_probed", "notes",
)


def upsert_company(conn, c):
    """Insert or update a company by name. `c` is a dict of column→value."""
    c = {**c, "last_probed": c.get("last_probed") or datetime.now().isoformat()}
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


def get_companies(conn, active_only=True, missions=None):
    q = "SELECT * FROM companies"
    conds, args = [], []
    if active_only:
        conds.append("active = 1")
    if missions:
        conds.append(f"mission_tier IN ({','.join('?' for _ in missions)})")
        args += list(missions)
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
    """Insert or refresh a job. Returns True if it was new."""
    now = datetime.now().isoformat()
    new = not job_exists(conn, j["job_id"])
    conn.execute(
        """INSERT INTO jobs
            (job_id, company_id, company_name, title, url, location, geo_mode,
             description, tech_bar_score, resume_fit_score, fit_reason,
             first_seen, last_seen, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             title=excluded.title, url=excluded.url, location=excluded.location,
             geo_mode=excluded.geo_mode, description=excluded.description,
             tech_bar_score=excluded.tech_bar_score,
             resume_fit_score=excluded.resume_fit_score,
             fit_reason=excluded.fit_reason, last_seen=excluded.last_seen,
             status=excluded.status""",
        (j["job_id"], j.get("company_id"), j.get("company_name"), j.get("title"),
         j.get("url"), j.get("location"), j.get("geo_mode"), j.get("description"),
         j.get("tech_bar_score"), j.get("resume_fit_score"), j.get("fit_reason"),
         now, now, j.get("status", "open")),
    )
    conn.commit()
    return new


def ranked_jobs(conn, limit=None):
    """Jobs joined to company mission, ranked by résumé fit then mission."""
    q = """
      SELECT j.*, c.mission_tier, c.mission_score, c.hq_location AS company_location
      FROM jobs j LEFT JOIN companies c ON j.company_id = c.id
      ORDER BY j.resume_fit_score DESC NULLS LAST, c.mission_score DESC NULLS LAST
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(q).fetchall()]
