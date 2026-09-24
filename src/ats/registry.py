"""Declarative ATS source registry.

One table describes, per ATS: how to build a fetch thunk from a store row,
the default seed tag discovery assigns, and the politeness pause for the
serial orchestrator. Every crawl path iterates STORE company rows — the
companies table is the single roster (config.py seed lists retired 2026-07;
manage the roster with discover.py, --add-board, or --import-companies).

The seed TAG (src/tags.py) is the scope a newly added company gets, and it
follows one rule: SWEEP for every ATS in LIGHTWEIGHT (a spec setting
`sweep`: a cheap board the sweep pulls whole), LOCAL for the boards that
are only worth querying per region. Paylocity and UltiPro seeded LOCAL
until 2026-09 because the
first boards found on them belonged to one user's local search; the tag
has meant crawl mechanics since the names were generalized, and both
boards are pulled whole in one or two requests like the rest of
LIGHTWEIGHT. tests/test_fetcher_parsers.py pins the rule.

Deliberately absent: ``src.store.CAPTURE_ATS`` ("capture"). A capture-only
company has no fetchable board -- its pages are saved by hand through
capture.py -- and store.crawlable_companies never hands such a row to any
crawl path, so it neither needs a thunk here nor counts as "unsupported":
an ATS name this table lacks is simply skipped by iter_store_sources.
"""

from src import tags
from src.match.filters import is_relevant

from .fetchers.board import BOARDS, board_for


def _engine_entry(board):
    """The ATS_REGISTRY row for a platform the engine runs: its sweep
    thunk, the seed tag its `sweep` flag picks, and that tag's pause."""
    sweep = bool(board.spec.get("sweep"))
    return (lambda n, s: lambda: board.jobs(s, n, gate=is_relevant),
            tags.SWEEP if sweep else tags.LOCAL, 0.5 if sweep else 1.0)


# ats -> (thunk(name, slug) -> fetch callable, seed tag, politeness pause)
#
# Every thunk passes `gate=is_relevant`: the fetchers themselves are
# ungated (gate=None keeps every posting), and this registry is where the
# profile's keyword filter is injected for the unvetted-board sweep. The
# company-vetted path (fetchers/company.py) calls the same fetchers with
# no gate and a location regex instead.
ATS_REGISTRY = {b.name: _engine_entry(b) for b in BOARDS.values() if b.fetchable}

# ATSes whose store rows a location-agnostic ("sweep") track pulls whole,
# and that seed the SWEEP tag: lightweight JSON APIs or single-page boards.
# The heavyweight boards stay location-scoped and seed LOCAL.
LIGHTWEIGHT = tuple(b.name for b in BOARDS.values() if b.spec.get("sweep"))


def seed_tag_for(ats):
    entry = ATS_REGISTRY.get(ats)
    return entry[1] if entry else None


def store_slug(company):
    """The registry-normalized slug for a store company row: a spec'd
    board's handle ("" when a column is empty), else the slug or the
    careers URL."""
    board = board_for(company.get("ats"))
    if board:
        return board.handle(company) or ""
    return company.get("slug") or company.get("careers_url") or ""


def iter_store_sources(companies, only=LIGHTWEIGHT):
    """Yield (ats, name, slug, thunk) for store company rows. `only=None`
    iterates every registered ATS (the classic orchestrator's sweep)."""
    for c in companies:
        ats = c.get("ats")
        if ats not in ATS_REGISTRY or (only and ats not in only):
            continue
        slug = store_slug(c)
        if not slug:
            continue
        mk, _tag, _pause = ATS_REGISTRY[ats]
        yield ats, c["name"], slug, mk(c["name"], slug)
