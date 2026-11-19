-- harvest_attempted_at: when a harvest pass last STARTED this board, whatever
-- came of it (src.crawl.harvest.plan orders boards stalest first, so a board
-- a budget cut off before it started is first next pass). Backfilled from
-- last_harvested_at, guarded on IS NULL, so a replay rewrites nothing.
--
-- Indexes, each for a plan that was a SCAN of companies (EXPLAIN QUERY PLAN
-- on the live roster scaled 20x, 2026-10-07):
--   * company_by_board's `ats = ? AND <handle column> IS ? COLLATE NOCASE`,
--     one per handle column (config.DEFAULT_HANDLE_COLUMNS and `handle`);
--   * company_id_by_name's `lower(name) = lower(?)`, run per ingested job.

ALTER TABLE companies ADD COLUMN harvest_attempted_at TEXT;

UPDATE companies SET harvest_attempted_at = last_harvested_at
WHERE harvest_attempted_at IS NULL AND last_harvested_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_companies_board_slug ON companies(ats, slug COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_companies_board_handle ON companies(ats, handle COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_companies_name_lower ON companies(lower(name));

DROP VIEW IF EXISTS companies_effective;

CREATE VIEW companies_effective AS
SELECT c.id, c.name, c.ats, c.slug, c.handle, c.careers_url,
       c.total_job_count,
       CASE WHEN c.mission_tier IS NOT NULL OR c.mission_score IS NOT NULL
            THEN c.mission_tier ELSE e.mission_tier END AS mission_tier,
       CASE WHEN c.mission_tier IS NOT NULL OR c.mission_score IS NOT NULL
            THEN c.mission_score ELSE e.mission_score END AS mission_score,
       CASE WHEN c.mission_tier IS NOT NULL OR c.mission_score IS NOT NULL
            THEN c.mission_reason ELSE e.mission_reason END AS mission_reason,
       c.tags,
       c.source,
       CASE WHEN COALESCE(c.review, e.review) = 'pending' THEN 0 ELSE c.active END AS active,
       c.last_probed, c.notes, c.local_job_count, c.created_at, c.miss_reason, c.miss_at,
       c.crawl_state, c.empty_streak, c.last_crawled_at, c.last_nonempty_at, c.next_crawl_at,
       c.last_harvested_at, c.harvest_attempted_at, c.employer_id,
       COALESCE(c.review, e.review, 'confirmed') AS review,
       COALESCE(c.watch, e.watch, 0) AS watch
FROM companies c LEFT JOIN employers e ON e.id = c.employer_id;
