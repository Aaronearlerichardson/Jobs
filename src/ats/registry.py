"""The sweep's fetch for one named board (tools/check_sources.py's probe),
and the seed tag a newly found board gets.

Every crawl path iterates STORE company rows: the companies table is the
single roster (manage it with discover.py, --add-board, or
--import-companies).

The seed TAG (src/tags.py) is the scope a newly added company gets: SWEEP
for a platform whose spec sets `sweep` (a cheap board the sweep pulls
whole), LOCAL for the boards only worth querying per region.
tests/test_board_specs.py pins the rule.

A capture-only company (``src.store.CAPTURE_ATS``) has no fetchable board
and store.harvestable_companies never hands one to a crawl path.

Notes:
    The config.py seed lists retired 2026-07. Paylocity and UltiPro seeded
    LOCAL until 2026-09 because the first boards found on them belonged to
    one user's local search; the tag has meant crawl mechanics since the
    names were generalized, and both are pulled whole in one or two
    requests like the rest of the sweep.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from src import tags
from src.match.filters import is_relevant
from src.rows import FetchedJob
from .board import board_for


def seed_tag_for(ats: str | None) -> str | None:
    """The tag a newly found `ats` board seeds: SWEEP where its spec sets
    `sweep`, else LOCAL; None for a platform no spec fetches."""
    board = board_for(ats)
    if not board:
        return None
    return tags.SWEEP if board.spec.sweep else tags.LOCAL


def sweep(ats: str, name: str,
          handle: str) -> Callable[[], Awaitable[list[FetchedJob]]] | None:
    """The sweep's fetch for one board: a thunk whose coroutine pulls it
    through the profile's keyword gate (the engine itself is ungated; the
    company-vetted path, board/company.py, passes a location regex
    instead). None for a platform no spec fetches."""
    board = board_for(ats)
    return (lambda: board.jobs(handle, name, gate=is_relevant)) if board else None
