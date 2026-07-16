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
import re
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
        # Per-axis fit sub-scores (jobcrawler/fit.py). resume_fit_score stays
        # the combined scalar; these expose the breakdown for querying/sorting.
        "fit_domain":      "REAL",
        "fit_function":    "REAL",
        "fit_stack":       "REAL",
        "fit_seniority":   "REAL",
        "fit_gates":       "TEXT",   # comma-joined tripped gate names, or NULL
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
             fit_domain, fit_function, fit_stack, fit_seniority, fit_gates,
             first_seen, last_seen, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
             fit_domain=COALESCE(excluded.fit_domain, fit_domain),
             fit_function=COALESCE(excluded.fit_function, fit_function),
             fit_stack=COALESCE(excluded.fit_stack, fit_stack),
             fit_seniority=COALESCE(excluded.fit_seniority, fit_seniority),
             fit_gates=COALESCE(excluded.fit_gates, fit_gates),
             last_seen=excluded.last_seen,
             status=excluded.status""",
        (j["job_id"], j.get("company_id"), j.get("company_name"), j.get("title"),
         j.get("url"), j.get("location"), j.get("track"), j.get("geo_mode"),
         remote, j.get("remote_signal"), j.get("neural_signal"),
         j.get("description"),
         j.get("resume_fit_score"), j.get("fit_reason"),
         j.get("fit_domain"), j.get("fit_function"), j.get("fit_stack"),
         j.get("fit_seniority"), j.get("fit_gates"),
         now, now, j.get("status", "open")),
    )
    conn.commit()
    return new


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
                allow_geo_modes=None):
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
    already qualifies them — e.g. a remote neural/BCI posting whose location
    string reads "Remote", not a Triangle/NC place name."""
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
        rows = [r for r in rows
                if location_re.search(r.get("location") or "")
                or (allow_geo_modes and r.get("geo_mode") in allow_geo_modes)]
    for r in rows:
        # A conglomerate's own mission score is ~0.05 (off-mission overall),
        # but a job here already passed the health keyword filter at crawl
        # time — so rank it at the keyword-vetted floor, not the company's
        # score, or its combined rank would be sunk unfairly.
        mission = r.get("mission_score")
        if config.is_multi_division(r.get("company_name")):
            mission = max(mission or 0.0, config.MULTI_DIVISION_MISSION_FLOOR)
        r["combined_score"] = combined_score(r.get("resume_fit_score"), mission)
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
        "neural_signal":   job.get("neural_signal"),
        "description":     (job.get("description") or "")[:2000],
        "resume_fit_score": job.get("resume_fit_score"),
        "fit_reason":      job.get("fit_reason"),
        "fit_gates":       job.get("fit_gates"),
        "fit_domain":      job.get("fit_domain"),
        "fit_function":    job.get("fit_function"),
        "fit_stack":       job.get("fit_stack"),
        "fit_seniority":   job.get("fit_seniority"),
    })
