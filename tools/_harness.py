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

import re
import sys

# An anti-bot wall or a rate limit says nothing about our parser: the
# endpoint is reachable and the code is fine, the request was refused.
# Often IP- or rate-based, so it may pass on a retry or from another
# network.
BLOCKED_RE = re.compile(
    r"\b(401|403|429|451)\b|captcha|cloudflare|forbidden|rate.?limit|"
    r"too many requests|access denied|challenge", re.I)


def console_utf8():
    """Make stdout carry the status glyphs.

    Windows consoles default to cp1252, which has none of ✅⚠️🚧❌, so a
    report that renders fine in CI dies on a UnicodeEncodeError on the
    machine it was written for. Best-effort: a stream that can't be
    reconfigured (a pipe, a captured buffer under pytest) is left alone.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def blame(note):
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
