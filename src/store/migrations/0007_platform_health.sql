-- One row per platform per crawl/harvest pass: how its boards fared.
-- errors = boards that failed with nothing; partial = incomplete or capped
-- snapshots that returned rows; empty = answered with no jobs and no error;
-- fill = JSON of the pooled per-field fill rates (ats.board.pager.FILL_FLOORS).

CREATE TABLE IF NOT EXISTS platform_health (
    pass_at TEXT NOT NULL,
    ats     TEXT NOT NULL,
    boards  INTEGER NOT NULL,
    errors  INTEGER NOT NULL,
    partial INTEGER NOT NULL,
    empty   INTEGER NOT NULL,
    jobs    INTEGER NOT NULL,
    fill    TEXT NOT NULL,
    PRIMARY KEY (ats, pass_at)
) WITHOUT ROWID;
