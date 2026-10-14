-- Retire the crawl's dormancy columns. The harvest is the one board-health
-- system now (companies.last_harvested_at / harvest_attempted_at rotation,
-- store.mark_harvested's dead-board cycle on miss_reason); nothing writes or
-- reads crawl_state, empty_streak, last_crawled_at or next_crawl_at any more.
-- last_nonempty_at stays: mark_harvested writes it and discovery.write and
-- ops.repair read it. The view is dropped first because it names the columns.

DROP VIEW IF EXISTS companies_effective;

ALTER TABLE companies DROP COLUMN crawl_state;
ALTER TABLE companies DROP COLUMN empty_streak;
ALTER TABLE companies DROP COLUMN last_crawled_at;
ALTER TABLE companies DROP COLUMN next_crawl_at;

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
       c.last_nonempty_at,
       c.last_harvested_at, c.harvest_attempted_at, c.employer_id,
       COALESCE(c.review, e.review, 'confirmed') AS review,
       COALESCE(c.watch, e.watch, 0) AS watch
FROM companies c LEFT JOIN employers e ON e.id = c.employer_id;
