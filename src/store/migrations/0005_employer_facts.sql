-- Employer-level facts with per-board overrides.
--
-- An employer decides what it is worth (the mission verdict), whether a person
-- has vetted it (review) and whether to watch it. Its boards (companies rows)
-- INHERIT those, and a board may OVERRIDE them: a large employer's
-- subdivisions can have genuinely different missions, scored on their own.
-- Both versions are stored and `companies_effective` is the one place that
-- says which wins (the board's value when it is set, else the employer's).
-- Every reader of these facts reads the view, never the two tables.
--
--   fact               employer (default)            board override (NULL = inherit)
--   mission tier,      employers.mission_tier,       companies.mission_tier,
--    score, reason      _score, _reason              _score, _reason: set when the board
--                                                    has a tier or a score, and then all
--                                                    three come from the board
--   review             employers.review              companies.review
--                      'pending' | NULL (confirmed)  'pending' | 'confirmed' | NULL
--   watch              employers.watch (0/1)         companies.watch (0/1 | NULL)
--
-- Stays on the board (crawl mechanics, never inherited): coordinates, counts,
-- dormancy and crawl state, miss_reason, the scope tags (`local`, `sweep` and
-- track store tags: HOW this board is crawled) and `active`, the board's own
-- crawl switch (a dead board, a parked or deactivated one). The view's
-- `active` is that switch AND the board is not pending review, so a pending
-- employer is never crawled whatever its boards say, and confirming it needs
-- no write to every board. Deactivating a dead board is board-only.
-- `tags` in the view is the board's scope tags plus `watch` and
-- `pending-review` when those are effective, so a reader asking
-- tags.has(row, tags.WATCH) keeps working. Confirm, reject and pending apply
-- to the whole employer (store.review); a board override is for one board.
--
-- Backfill: each employer takes the values of its primary board (a scored
-- board first, then the most jobs, then the lowest id), and a board whose own
-- values differ (tier or score, review, watch) keeps them as an override, so
-- the effective values equal today's. A board with NO verdict beside a scored
-- primary cannot be told from "inherit" and inherits. The old `watch` and
-- `pending-review` tag tokens are retired from companies.tags.
--
-- Adding a column to companies means recreating the view (and
-- tests/test_store.py checks that it lists every column).

ALTER TABLE employers ADD COLUMN mission_tier TEXT;
ALTER TABLE employers ADD COLUMN mission_score REAL;
ALTER TABLE employers ADD COLUMN mission_reason TEXT;
ALTER TABLE employers ADD COLUMN review TEXT;
ALTER TABLE employers ADD COLUMN watch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE companies ADD COLUMN review TEXT;
ALTER TABLE companies ADD COLUMN watch INTEGER;

CREATE TEMP TABLE _emp_todo AS
SELECT id FROM employers
WHERE mission_tier IS NULL AND mission_score IS NULL AND mission_reason IS NULL
  AND review IS NULL AND watch = 0;

CREATE TEMP TABLE _emp_primary AS
SELECT employer_id, company_id FROM (
    SELECT c.employer_id, c.id AS company_id,
           ROW_NUMBER() OVER (PARTITION BY c.employer_id
                              ORDER BY (c.mission_tier IS NULL AND c.mission_score IS NULL),
                                       COALESCE(n.jobs, 0) DESC, c.id) AS rn
    FROM companies c
    LEFT JOIN (SELECT company_id, COUNT(*) AS jobs FROM jobs GROUP BY company_id) n
           ON n.company_id = c.id
    WHERE c.employer_id IN (SELECT id FROM _emp_todo)
) WHERE rn = 1;

UPDATE employers SET
    mission_tier   = (SELECT c.mission_tier   FROM _emp_primary p JOIN companies c ON c.id = p.company_id
                      WHERE p.employer_id = employers.id),
    mission_score  = (SELECT c.mission_score  FROM _emp_primary p JOIN companies c ON c.id = p.company_id
                      WHERE p.employer_id = employers.id),
    mission_reason = (SELECT c.mission_reason FROM _emp_primary p JOIN companies c ON c.id = p.company_id
                      WHERE p.employer_id = employers.id),
    review = (SELECT CASE WHEN (',' || COALESCE(c.tags, '') || ',') LIKE '%,pending-review,%'
                          THEN 'pending' END
              FROM _emp_primary p JOIN companies c ON c.id = p.company_id
              WHERE p.employer_id = employers.id),
    watch  = COALESCE((SELECT (',' || COALESCE(c.tags, '') || ',') LIKE '%,watch,%'
                       FROM _emp_primary p JOIN companies c ON c.id = p.company_id
                       WHERE p.employer_id = employers.id), 0)
WHERE id IN (SELECT id FROM _emp_todo);

-- A board that differs keeps its own value, one that matches inherits.
UPDATE companies SET
    review = CASE
        WHEN (',' || COALESCE(tags, '') || ',') LIKE '%,pending-review,%'
             AND (SELECT review FROM employers e WHERE e.id = companies.employer_id) IS NULL
            THEN 'pending'
        WHEN (',' || COALESCE(tags, '') || ',') NOT LIKE '%,pending-review,%'
             AND (SELECT review FROM employers e WHERE e.id = companies.employer_id) = 'pending'
            THEN 'confirmed'
    END,
    watch = CASE
        WHEN (',' || COALESCE(tags, '') || ',') LIKE '%,watch,%'
             AND (SELECT watch FROM employers e WHERE e.id = companies.employer_id) = 0 THEN 1
        WHEN (',' || COALESCE(tags, '') || ',') NOT LIKE '%,watch,%'
             AND (SELECT watch FROM employers e WHERE e.id = companies.employer_id) = 1 THEN 0
    END
WHERE employer_id IN (SELECT id FROM _emp_todo);

UPDATE companies SET mission_tier = NULL, mission_score = NULL, mission_reason = NULL
WHERE employer_id IN (SELECT id FROM _emp_todo)
  AND EXISTS (SELECT 1 FROM employers e WHERE e.id = companies.employer_id
              AND e.mission_tier IS companies.mission_tier
              AND e.mission_score IS companies.mission_score);

UPDATE companies SET tags = (
    SELECT group_concat(j.value, ',')
    FROM json_each('["' || replace(companies.tags, ',', '","') || '"]') j
    WHERE j.value NOT IN ('watch', 'pending-review'))
WHERE employer_id IN (SELECT id FROM _emp_todo)
  AND ((',' || COALESCE(tags, '') || ',') LIKE '%,watch,%'
       OR (',' || COALESCE(tags, '') || ',') LIKE '%,pending-review,%');

DROP TABLE _emp_primary;
DROP TABLE _emp_todo;

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
       NULLIF(trim(COALESCE(c.tags, '')
                   || CASE WHEN COALESCE(c.review, e.review) = 'pending' THEN ',pending-review' ELSE '' END
                   || CASE WHEN COALESCE(c.watch, e.watch, 0) THEN ',watch' ELSE '' END, ','), '') AS tags,
       c.source,
       CASE WHEN COALESCE(c.review, e.review) = 'pending' THEN 0 ELSE c.active END AS active,
       c.last_probed, c.notes, c.local_job_count, c.created_at, c.miss_reason, c.miss_at,
       c.crawl_state, c.empty_streak, c.last_crawled_at, c.last_nonempty_at, c.next_crawl_at,
       c.last_harvested_at, c.employer_id,
       COALESCE(c.review, e.review, 'confirmed') AS review,
       COALESCE(c.watch, e.watch, 0) AS watch
FROM companies c LEFT JOIN employers e ON e.id = c.employer_id;

-- A board moved to another employer (link_employers, a dedup merge) keeps what
-- it was: where it was inheriting a fact the new employer holds differently,
-- the old employer's value is pinned on the board.
CREATE TRIGGER IF NOT EXISTS companies_employer_moved AFTER UPDATE OF employer_id ON companies
WHEN NEW.employer_id IS NOT OLD.employer_id AND OLD.employer_id IS NOT NULL
BEGIN
    UPDATE companies SET
        mission_tier = CASE
            WHEN OLD.mission_tier IS NULL AND OLD.mission_score IS NULL
             AND (SELECT mission_tier IS NOT (SELECT x.mission_tier FROM employers x WHERE x.id = NEW.employer_id)
                         OR mission_score IS NOT (SELECT x.mission_score FROM employers x WHERE x.id = NEW.employer_id)
                  FROM employers WHERE id = OLD.employer_id)
            THEN (SELECT mission_tier FROM employers WHERE id = OLD.employer_id)
            ELSE mission_tier END,
        mission_score = CASE
            WHEN OLD.mission_tier IS NULL AND OLD.mission_score IS NULL
             AND (SELECT mission_tier IS NOT (SELECT x.mission_tier FROM employers x WHERE x.id = NEW.employer_id)
                         OR mission_score IS NOT (SELECT x.mission_score FROM employers x WHERE x.id = NEW.employer_id)
                  FROM employers WHERE id = OLD.employer_id)
            THEN (SELECT mission_score FROM employers WHERE id = OLD.employer_id)
            ELSE mission_score END,
        mission_reason = CASE
            WHEN OLD.mission_tier IS NULL AND OLD.mission_score IS NULL
             AND (SELECT mission_tier IS NOT (SELECT x.mission_tier FROM employers x WHERE x.id = NEW.employer_id)
                         OR mission_score IS NOT (SELECT x.mission_score FROM employers x WHERE x.id = NEW.employer_id)
                  FROM employers WHERE id = OLD.employer_id)
            THEN (SELECT mission_reason FROM employers WHERE id = OLD.employer_id)
            ELSE mission_reason END,
        review = CASE
            WHEN OLD.review IS NULL
             AND (SELECT COALESCE(review, '') FROM employers WHERE id = OLD.employer_id)
                 != (SELECT COALESCE(review, '') FROM employers WHERE id = NEW.employer_id)
            THEN COALESCE((SELECT review FROM employers WHERE id = OLD.employer_id), 'confirmed')
            ELSE review END,
        watch = CASE
            WHEN OLD.watch IS NULL
             AND (SELECT watch FROM employers WHERE id = OLD.employer_id)
                 != (SELECT watch FROM employers WHERE id = NEW.employer_id)
            THEN (SELECT watch FROM employers WHERE id = OLD.employer_id)
            ELSE watch END
    WHERE id = NEW.id;
END;
