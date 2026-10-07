-- One generic `handle` column for a board handle of several parts, joined by
-- its spec's `handle.sep` (config.BOARDS): Workday's "tenant|pod|site".
--
-- wd_tenant, wd_pod and wd_site are DEPRECATED: nothing reads or writes them
-- from here on, and the view no longer lists them. They stay in the table,
-- unchanged, so a rollback to code that reads them loses nothing.
--
-- Backfilled from them, guarded on `handle IS NULL`, so a replay rewrites
-- nothing. "|" is the workday spec's `handle.sep`; a missing part is left
-- empty, which the engine reads as no handle, as it read the NULL column.

ALTER TABLE companies ADD COLUMN handle TEXT;

UPDATE companies
SET handle = wd_tenant || '|' || COALESCE(CAST(wd_pod AS TEXT), '') || '|'
             || COALESCE(wd_site, '')
WHERE handle IS NULL AND wd_tenant IS NOT NULL AND ats = 'workday';

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
       c.last_harvested_at, c.employer_id,
       COALESCE(c.review, e.review, 'confirmed') AS review,
       COALESCE(c.watch, e.watch, 0) AS watch
FROM companies c LEFT JOIN employers e ON e.id = c.employer_id;
