"""The ATS sweep: which store rows the lightweight sweep pulls, how, and
the seed tag a newly found board gets.

Every crawl path iterates STORE company rows: the companies table is the
single roster (manage it with discover.py, --add-board, or
--import-companies).

The seed TAG (src/tags.py) is the scope a newly added company gets: SWEEP
for a platform whose spec sets `sweep` (a cheap board the sweep pulls
whole), LOCAL for the boards only worth querying per region.
tests/test_boards_spec.py pins the rule.

A capture-only company (``src.store.CAPTURE_ATS``) has no fetchable board
and store.crawlable_companies never hands one to a crawl path; like any
ATS no spec fetches, iter_store_sources skips it.

Notes:
    The config.py seed lists retired 2026-07. Paylocity and UltiPro seeded
    LOCAL until 2026-09 because the first boards found on them belonged to
    one user's local search; the tag has meant crawl mechanics since the
    names were generalized, and both are pulled whole in one or two
    requests like the rest of the sweep.
"""

from src import tags
from src.match.filters import is_relevant

from .board import board_for


def seed_tag_for(ats):
    """The tag a newly found `ats` board seeds: SWEEP where its spec sets
    `sweep`, else LOCAL; None for a platform no spec fetches."""
    board = board_for(ats)
    if not board:
        return None
    return tags.SWEEP if board.spec.get("sweep") else tags.LOCAL


def sweep(ats, name, handle):
    """The sweep's fetch for one board: a thunk pulling it through the
    profile's keyword gate (the engine itself is ungated; the
    company-vetted path, board/company.py, passes a location regex
    instead). None for a platform no spec fetches."""
    board = board_for(ats)
    return (lambda: board.jobs(handle, name, gate=is_relevant)) if board else None


def iter_store_sources(companies):
    """Yield (ats, name, handle, thunk) for the store rows on a platform
    the lightweight sweep pulls whole (its spec's `sweep`) that name a
    board."""
    for c in companies:
        board = board_for(c.get("ats"))
        handle = board.handle(c) if board and board.spec.get("sweep") else None
        if handle:
            yield board.name, c["name"], handle, sweep(board.name, c["name"], handle)
