"""pytest fixtures that reach the doctests under src/ as well as the tests.

tests/conftest.py imports them; the doctests in src/ are outside its
directory, so they get them from here.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Iterator

import pytest


def _tracking[**P](real: Callable[P, sqlite3.Connection],
                   opened: list[sqlite3.Connection]) -> Callable[P, sqlite3.Connection]:
    """`real`, recording each connection it returns in `opened`."""
    def tracked(*args: P.args, **kwargs: P.kwargs) -> sqlite3.Connection:
        conn = real(*args, **kwargs)
        opened.append(conn)
        return conn
    return tracked


@pytest.fixture(autouse=True)
def _close_stores(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close every sqlite connection a test or doctest opened, when it ends.

    They open a store and let it go; from Python 3.13 the garbage collector
    says so ("ResourceWarning: unclosed database"), a hundred times a run
    under --cov, each attributed to whichever item happened to be running
    when it was collected. `sqlite3.connect` is the one place every route
    (`store.connect`, a name imported from it, a test's own) ends up.
    Closing again is harmless, so one that closed its own store is
    unaffected; one opened in another thread cannot be closed from here and
    is left as it was.
    """
    opened: list[sqlite3.Connection] = []
    real = sqlite3.connect

    monkeypatch.setattr(sqlite3, "connect", _tracking(real, opened))
    yield
    for conn in opened:
        with contextlib.suppress(sqlite3.ProgrammingError):
            conn.close()
