-- Cross-board duplicates: the same opening listed on two boards of one
-- employer. `dup_of` is the jobs.id of the surviving row (NULL = this row
-- stands on its own); store.flag_duplicate_jobs maintains it and counts,
-- digests and notifications skip rows that carry it. Nothing is deleted.

ALTER TABLE jobs ADD COLUMN dup_of INTEGER;

CREATE INDEX IF NOT EXISTS ix_jobs_dup ON jobs(dup_of) WHERE dup_of IS NOT NULL;

-- The open postings that count once: what a total or a "new today" tally reads.
CREATE VIEW IF NOT EXISTS live_jobs AS SELECT * FROM open_jobs WHERE dup_of IS NULL;
