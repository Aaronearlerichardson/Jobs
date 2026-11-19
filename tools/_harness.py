"""Shared scaffolding for the canaries in tools/.

`check_boards.py` and `check_sources.py` ask the same question of two
different populations -- is this source alive, and if not, whose fault is
it -- so they had grown the same three pieces of apparatus independently:
a UTF-8 console, a vocabulary for "not our bug", and a way to widen the
keyword filter (that last one now lives in `config.widen_keywords`, since
the test suite needs it too). A third pattern, `_BROKEN_RE`, turned out to
be dead in both scripts -- "broken" is what a failure is called when it is
not blocked, never something matched for -- so it is gone rather than
moved here.

The classification vocabulary is the part that mattered. The two copies of
the "blocked" pattern had already drifted -- one of them learned about
anti-bot *challenge* pages and the other never did -- which meant the same
CloudFront response was a rate limit in one report and a broken parser in
the other. One pattern now.

The `sys.path` line at the top of each script stays where it is: it is what
makes importing this module possible, so it cannot live in it.
"""

from __future__ import annotations

import re
import sqlite3
import io
import sys
from collections.abc import Callable, Sequence
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.claude.fit import FitResult
    from src.config import RuntimeTrack

# An anti-bot wall or a rate limit says nothing about our parser: the
# endpoint is reachable and the code is fine, the request was refused.
# Often IP- or rate-based, so it may pass on a retry or from another
# network.
BLOCKED_RE = re.compile(
    r"\b(401|403|429|451)\b|captcha|cloudflare|forbidden|rate.?limit|"
    r"too many requests|access denied|challenge", re.I)


def console_utf8() -> None:
    """Make stdout carry the status glyphs.

    Windows consoles default to cp1252, which has none of ✅⚠️🚧❌, so a
    report that renders fine in CI dies on a UnicodeEncodeError on the
    machine it was written for. Best-effort: a stream that can't be
    reconfigured (a pipe, a captured buffer under pytest) is left alone.
    """
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def blame(note: str | None) -> str:
    """Whose fault a failure is, from a fetcher's diagnostic text.

    >>> blame("HTTPError: 429 Too Many Requests")
    'blocked'
    >>> blame("JSONDecodeError: Expecting value")
    'broken'

    Nothing to go on reads as broken, not blocked -- an unexplained empty
    result is the case worth looking at, and calling it "blocked" would
    file it under "not our problem" unread:

    >>> blame("")
    'broken'
    """
    return "blocked" if BLOCKED_RE.search(note or "") else "broken"


def open_ro(path: str | Path) -> sqlite3.Connection:
    """The store at `path` through a mode=ro URI (no migration, no WAL
    pragma): SQLite refuses any write. Rows are `sqlite3.Row`."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def default_track() -> RuntimeTrack:
    """The profile's default-flagged track, else its first."""
    from src import config
    return config.UI_TRACKS[config.DEFAULT_TRACK or next(iter(config.UI_TRACKS))]


async def score_rows(rows: Sequence[dict[str, Any]], label: Callable[[dict[str, Any]], str],
                     workers: int | None = None) -> list[tuple[dict[str, Any], FitResult]]:
    """Each row (`title`, `description`, `location`) with its current fit
    (paid: one Claude call per row), in completion order."""
    from src.claude.fit import score_resume_fit
    from src.net.parallel import DEFAULT_WORKERS, fan_out

    async def one(r: dict[str, Any]) -> FitResult:
        return await score_resume_fit(r["title"] or "", r["description"] or "",
                                      location=r["location"] or "")
    async with aclosing(fan_out(rows, one, label, workers or DEFAULT_WORKERS, with_item=True)) as got:
        return [pair async for pair in got]
