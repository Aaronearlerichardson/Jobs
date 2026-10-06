-- `companies_effective.tags` is the board's OWN tags again.
--
-- 0005 made the view synthesize `watch` and `pending-review` tokens into
-- `tags` from the review and watch facts, so readers kept testing tokens
-- while writers stripped them: one fact in two representations. The facts are
-- the `review` and `watch` columns of the view (the board's value, else its
-- employer's: unchanged), and `tags` is the board's scope tags and nothing
-- else. `active` stays "the board's switch AND not pending".
--
-- A stray token in a board's raw tags (an older writer) is dropped; the facts
-- were never read from it.

DROP VIEW IF EXISTS companies_effective;

CREATE VIEW companies_effective AS
SELECT c.id, c.name, c.ats, c.slug, c.wd_tenant, c.wd_pod, c.wd_site, c.careers_url,
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

UPDATE companies SET tags = (
    SELECT group_concat(j.value, ',')
    FROM json_each('["' || replace(companies.tags, ',', '","') || '"]') j
    WHERE j.value NOT IN ('watch', 'pending-review'))
WHERE (',' || COALESCE(tags, '') || ',') LIKE '%,watch,%'
   OR (',' || COALESCE(tags, '') || ',') LIKE '%,pending-review,%';
