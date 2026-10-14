-- jobs.source retired. A feed is a roster row now (companies.source = 'feed',
-- ats = the feed family; store.sync_feed_companies), so every job's origin is
-- its company_id and the column that named a company-less row's feed has no
-- reader. The partial index goes first: SQLite will not drop an indexed column.
--
-- board_employers is rebuilt without the feed rows: a feed row is no employer,
-- and its postings name their own (jobs.company_name), which upsert_job and
-- realign_job_names must not overwrite with the feed's display name.

DROP INDEX IF EXISTS ix_jobs_source;

ALTER TABLE jobs DROP COLUMN source;

DROP VIEW IF EXISTS board_employers;

CREATE VIEW board_employers AS
SELECT c.id AS company_id, c.employer_id, COALESCE(e.name, c.name) AS employer_name
FROM companies c LEFT JOIN employers e ON e.id = c.employer_id
WHERE c.source IS NOT 'feed';
