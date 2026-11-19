-- The store's schema as of versioning. Every statement is IF NOT EXISTS, so
-- it is also what brings a store that predates versioning up to date (see
-- migrate._adopt_unversioned, which adds the columns such a file lacks first).
--
-- A change to the schema is a NEW numbered file beside this one, never an
-- edit of an applied one; migrate.migrate() runs the files a store has not
-- seen and records the highest in PRAGMA user_version.

CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    ats             TEXT,               -- greenhouse|lever|ashby|workday|...
    slug            TEXT,               -- board slug (non-workday)
    wd_tenant       TEXT,               -- workday triple
    wd_pod          INTEGER,
    wd_site         TEXT,
    careers_url     TEXT,
    local_job_count INTEGER DEFAULT 0,  -- openings inside your [locality]
    total_job_count INTEGER DEFAULT 0,
    mission_tier    TEXT,               -- a tier name from profile [mission]
    mission_score   REAL,               -- 0..1 (alignment with what you care about)
    mission_reason  TEXT,
    tags            TEXT,               -- comma scope tokens; see src/tags.py
    source          TEXT,               -- how it was discovered
    active          INTEGER DEFAULT 1,  -- crawl this company?
    last_probed     TEXT,
    notes           TEXT,
    created_at      TEXT,               -- first time this row was inserted; NULL
                                        -- predates the column (see roster_growth)
    miss_reason     TEXT,               -- why it is not crawlable (see MISS_REASONS);
    miss_at         TEXT,               --   rows carrying these are always active=0
    -- Crawl scheduling (record_crawl_outcome / crawlable_companies): a company
    -- that never produces a job stops being fetched on every crawl.
    crawl_state       TEXT,             -- active|dormant|off (NULL = active)
    empty_streak      INTEGER,          -- consecutive empty DAYS
    last_crawled_at   TEXT,
    last_nonempty_at  TEXT,
    next_crawl_at     TEXT,             -- dormant rows wake at/after this
    -- Last SUCCESSFUL whole-board pull by the background harvester
    -- (src/crawl/harvest.py); independent of the crawl stamps above.
    last_harvested_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY,
    job_id           TEXT UNIQUE NOT NULL,  -- source-stable id
    company_id       INTEGER REFERENCES companies(id),
    company_name     TEXT,
    title            TEXT,
    url              TEXT,
    location         TEXT,
    track            TEXT,                  -- comma-separated SET of track names
    geo_mode         TEXT,                  -- onsite|remote
    remote_eligible  INTEGER,               -- 1 when the remote filter passed
    remote_signal    TEXT,                  -- phrase/hint that marked it remote
    anchor_signal    TEXT,                  -- the CORE keyword that anchored it
    description      TEXT,
    desc_checked_at  TEXT,                  -- last FAILED description backfill
                                            -- attempt (ops.backfill_* skip
                                            -- recently-checked rows)
    resume_fit_score REAL,
    fit_reason       TEXT,
    first_seen       TEXT,                  -- when WE noticed it
    last_seen        TEXT,
    status           TEXT DEFAULT 'open',   -- open|closed (see sync_job_statuses)
    -- Stamped by the background harvester when it stored or refreshed the row.
    -- A harvested row arrives with NO track and NO score; the crawl adopts it
    -- the next time that company's board comes round (see crawl_seen).
    harvested_at     TEXT,
    -- Harvest triage (src/crawl/triage.py). NULL = not yet judged; 'ok' =
    -- surfaced into the track set; anything else names the cheapest gate that
    -- dropped the row. triage_detail is the per-track record.
    triage_status    TEXT,
    triage_detail    TEXT,
    triaged_at       TEXT,
    -- Per-axis fit sub-scores (src/claude/fit.py); resume_fit_score stays the
    -- combined scalar.
    fit_domain       REAL,
    fit_function     REAL,
    fit_stack        REAL,
    fit_seniority    REAL,
    fit_gates        TEXT,                  -- comma-joined tripped gate names, or NULL
    fit_model        TEXT,                  -- model id that wrote the current score
    closed_at        TEXT,                  -- when status flipped to 'closed'
    posted_at        TEXT,                  -- when it went up (first-known wins)
    -- The user's decision on this job (see DISPOSITIONS): drives ranking
    -- exclusion, the digest pipeline section, and few-shot calibration.
    disposition      TEXT,
    disposition_note TEXT,
    disposition_at   TEXT,
    -- Consecutive closure probes that could not verify the row either way;
    -- NULL reads as 0 (src.ops.status.check_closed_jobs).
    probe_streak     INTEGER,
    -- Application-pipeline tracking. applied_at is stamped the FIRST time a
    -- row is marked applied and never again; the rest are user-entered.
    applied_at       TEXT,
    followup_at      TEXT,                  -- YYYY-MM-DD, the next nudge
    contact          TEXT,                  -- recruiter / hiring manager / referrer
    referral         INTEGER,               -- 0/1
    outcome_reason   TEXT                   -- one of OUTCOME_REASONS
);

-- Company names a person rejected from the review queue. Keyed by the
-- normalized name (see _name_key), so one rejected spelling blocks the rest.
CREATE TABLE IF NOT EXISTS name_blocklist (
    key      TEXT PRIMARY KEY,
    name     TEXT,              -- the spelling that was rejected
    reason   TEXT,
    added_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS ix_jobs_track   ON jobs(track);
-- upsert_job probes `url` for every NEW row (the re-key path that catches a
-- posting arriving under a changed id scheme). Unindexed, that probe was a
-- full scan of a table whose rows carry whole job descriptions: 0.23s each
-- on a 240 MB store, INSIDE the batch transaction. A 183-job board therefore
-- held the single write lock for ~40s, and every other writer -- the other
-- harvest threads, and the web UI's own edits -- timed out against
-- BUSY_TIMEOUT_S with "database is locked" (2026-09-10 harvest logs).
CREATE INDEX IF NOT EXISTS ix_jobs_url     ON jobs(url);
-- triage_pending selects the harvester's unjudged rows on this column.
CREATE INDEX IF NOT EXISTS ix_jobs_triage  ON jobs(triage_status);
