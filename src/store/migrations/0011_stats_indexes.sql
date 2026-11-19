-- The Stats tiles (web/routes.py api_stats) seek instead of scanning a table
-- whose rows carry whole descriptions: 2.7 s a call before, ~26 ms after
-- (2026-10-08, 227k jobs). The partial index's WHERE is written exactly as
-- the open_jobs view writes it, so a query through live_jobs can use it.

CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);

CREATE INDEX IF NOT EXISTS ix_jobs_applied ON jobs(applied_at)
    WHERE applied_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_jobs_live_stats ON jobs(dup_of, first_seen, posted_at)
    WHERE COALESCE(status, 'open') != 'closed';
