"""The three passes over the roster's boards.

    runner.py   the daily crawl: narrow, scoped, scores what survives
    harvest.py  the background whole-board pull: everything, unscored
    triage.py   what the harvester stored, gated cheapest-first
    page_capture.py  jobs parsed out of pages you captured by hand

The crawl is the fast path and the harvest is the thorough one; triage is
what makes the thorough one affordable.
"""
