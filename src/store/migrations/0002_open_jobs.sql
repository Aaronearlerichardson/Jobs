-- One definition of "a job that is still open" (a NULL status reads as open,
-- as it always has), and the indexes that make the queries over it, and over
-- dispositioned rows, seek instead of scanning a table whose rows carry whole
-- descriptions. Each partial index's WHERE is written exactly as the view
-- writes it, so a query through the view can use it.

CREATE VIEW IF NOT EXISTS open_jobs AS
SELECT * FROM jobs WHERE COALESCE(status, 'open') != 'closed';

-- Open postings and the best résumé fit per company (the roster's "watch
-- this company" signal).
CREATE VIEW IF NOT EXISTS company_open_stats AS
SELECT company_id, COUNT(*) AS open_jobs, MAX(resume_fit_score) AS best_fit
FROM open_jobs WHERE company_id IS NOT NULL GROUP BY company_id;

-- The pipeline and follow-up reads: a few hundred dispositioned rows out of
-- ~120k (117 ms scans before, ~4 ms after).
CREATE INDEX IF NOT EXISTS ix_jobs_disposition ON jobs(disposition_at)
    WHERE disposition IS NOT NULL;

-- company_open_stats (271 ms -> ~9 ms), covering.
CREATE INDEX IF NOT EXISTS ix_jobs_open_company ON jobs(company_id, resume_fit_score)
    WHERE COALESCE(status, 'open') != 'closed';

-- The closed-probe's "not seen lately" selection.
CREATE INDEX IF NOT EXISTS ix_jobs_open_seen ON jobs(COALESCE(last_seen, first_seen, ''))
    WHERE COALESCE(status, 'open') != 'closed';
