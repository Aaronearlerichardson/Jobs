"""The three passes over the roster's boards.

    harvest.py  the whole-board and feed pull (a crawl is a scoped run): everything stored, then triaged
    triage.py   what the harvester stored, gated cheapest-first
    page_capture.py  jobs parsed out of pages you captured by hand

Triage is what makes the whole-board pull affordable.
"""

from __future__ import annotations
