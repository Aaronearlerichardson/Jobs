"""The operations a front end can run; src/dispatch/registry.py names them.

Every op takes the track config `t` (a config.UI_TRACKS entry; None = the
default local-engine track), so any front end can run it on any track.

    maintenance.py  what the ops share: the track's store, the ranking and
                    digest, the crawl's per-company gate and score
    status.py       open/closed: the board-snapshot sync, the closed-URL probe
    scoring.py      the unscored self-heal, the rescore, the deep verify
    backfill.py     missing descriptions, from each company's own board
    ingest.py       external postings (capture, NLx) and manual adds
    repair.py       roster repair: dead boards, re-resolution, slug renames
    rekey.py        job-id migration after a spec's id rule changes
    roster.py       the composite targets: open a track's store, call one
                    thing, report what it did
"""
