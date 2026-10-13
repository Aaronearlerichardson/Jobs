"""The three passes over the roster's boards.

    runner.py   a crawl of one track: the harvest scoped to it, then the
                sources off the roster
    harvest.py  the whole-board pull: everything stored, then triaged
    triage.py   what the harvester stored, gated cheapest-first
    page_capture.py  jobs parsed out of pages you captured by hand

Triage is what makes the whole-board pull affordable.
"""

from __future__ import annotations
