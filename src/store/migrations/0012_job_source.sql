-- jobs.source: the feed instance a row came from ("remoteok", "rss:<label>",
-- "getro:<host>", ...), NULL for a roster-board row. A feed has no company, so
-- the source is what a feed snapshot closes against (store.sync_job_statuses)
-- and what makes a company-less row pending for triage (store.triage_pending).
--
-- The partial index holds only feed rows (a few thousand out of ~200k), so the
-- two queries that name the column seek instead of scanning a table whose rows
-- carry whole descriptions: the snapshot's `WHERE source = ?` and the pending
-- probe's `source IS NOT NULL`.

ALTER TABLE jobs ADD COLUMN source TEXT;

CREATE INDEX IF NOT EXISTS ix_jobs_source ON jobs(source) WHERE source IS NOT NULL;
