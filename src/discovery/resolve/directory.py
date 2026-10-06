"""The board directory's name index, the half of `discovery.board_directory`
the resolver reads (so store-free). `base_url` is [sources.board_directory]'s.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from src import config
from src.ats.board import BOARDS
from src.ats.signatures import detect, pack
from src.match.names import name_key
from src.net.util import cache_dir
from src.runstate import per_run
from .probes import slug_keyed


class Detected(NamedTuple):
    kind: str                   # "fetchable" or "lead"
    ats: str
    handle: Any
    careers_url: str | None


def board_of(url: str | None, label: str = "", slug: str = "") -> Detected | None:
    """The board a posting or board URL names (`signatures.detect`), never
    the dataset's `label`. Failing that, a `label` that is a fetchable
    platform names the board: by `slug`, else the URL's host, where the
    handle is one slug; by the URL's origin, where it is the careers URL.
    The live read afterwards rejects a wrong guess.

    >>> board_of("https://boards.greenhouse.io/acmebio/jobs/1")
    Detected(kind='fetchable', ats='greenhouse', handle='acmebio', careers_url=None)
    >>> board_of("https://acme.wd5.myworkdayjobs.com/en-US/External/job/x").handle
    ('acme', 5, 'External')
    >>> board_of("https://example.com/careers", "no-such-platform") is None
    True
    >>> board_of("https://careers.acme.org/job/94095", "phenom")
    Detected(kind='fetchable', ats='phenom', handle='careers.acme.org', careers_url=None)
    >>> board_of("https://career.acme.org/job/x/1", "successfactors")
    Detected(kind='fetchable', ats='successfactors', handle=None, careers_url='https://career.acme.org')
    """
    hit = detect("", url or "")
    if hit:
        kind, ats, handle = hit
        careers = (pack(ats, handle, url or "")["careers_url"]
                   if kind == "fetchable" and not slug_keyed(BOARDS[ats]) else None)
        return Detected(kind, ats, handle, careers)
    board, parts = BOARDS.get(label), urlsplit(url or "")
    if not (board and board.fetchable and not board.multi_column and parts.hostname):
        return None
    if not slug_keyed(board):
        return Detected("fetchable", label, None, f"{parts.scheme}://{parts.netloc}")
    slug = slug or parts.hostname
    return Detected("fetchable", label, slug, None) if " " not in slug else None


def is_url(base: str) -> bool:
    return base.startswith(("http://", "https://"))


def locate(rel: str) -> str:
    """The directory's file `rel`: its URL, or its path under a local base."""
    base = config.BOARD_DIRECTORY.base_url
    return f"{base.rstrip('/')}/{rel}" if is_url(base) else str(Path(base) / rel)


def cache_path(rel: str) -> Path:
    return cache_dir("board_directory", rel.replace("/", "_"))


def query(src: str, sql: str, args: Sequence[str], remote: bool) -> list[tuple[Any, ...]]:
    """The rows of `sql` over the parquet file or URL `src` (`?` in `sql`
    is `src`, then `args`)."""
    import duckdb
    con = duckdb.connect()
    try:
        if remote:
            con.load_extension("httpfs")
        return con.execute(sql, [src, *args]).fetchall()
    finally:
        con.close()


class Companies(NamedTuple):
    """companies.parquet, indexed."""
    named: list[tuple[Detected, str]]                    # each fetchable board, its name
    by_name: dict[str, list[tuple[str, Any, str]]]       # name_key -> (ats, handle, url)


def _load_companies() -> Companies:
    """companies.parquet's boards by name, each row's URL read through
    `board_of`. Empty when the file cannot be read."""
    cached = cache_path("companies.parquet")
    src = (str(cached) if is_url(config.BOARD_DIRECTORY.base_url) and cached.exists()
           else locate("companies.parquet"))
    try:
        rows = query(src, "SELECT ats, name, slug, url FROM read_parquet(?)", (), is_url(src))
    except Exception as e:      # no file, no httpfs: the lookup answers nothing
        print(f"    [!] board directory companies: {str(e).splitlines()[0][:100]}")
        return Companies([], {})
    named: list[tuple[Detected, str]] = []
    by_name: defaultdict[str, list[tuple[str, Any, str]]] = defaultdict(list)
    for label, name, slug, url in rows:
        found = board_of(url, label or "", slug or "")
        if name and found and found.kind == "fetchable":
            named.append((found, name))
            by_name[name_key(name)].append((found.ats, found.handle, url))
    return Companies(named, dict(by_name))


#: The directory's boards by name, built at the run's first use.
index = per_run(_load_companies)


def lookup_name(name: str) -> list[tuple[str, Any, str]]:
    """(ats, handle, url) of each board the directory lists under exactly
    `name` (`name_key`, suffixes kept), for a caller to validate: some are
    other companies that share the name. The first call of a run reads
    companies.parquet (the cache after an `import_boards`, else the
    dataset) and blocks while it indexes it.

    >>> lookup_name("")
    []
    """
    key = name_key(name)
    return list(index().by_name.get(key, [])) if key else []


_building = per_run(asyncio.Lock)


async def find_boards(name: str) -> list[tuple[str, Any, str]]:
    """`lookup_name` for the event loop: the index builds once per run, in a
    thread, while concurrent callers wait.

    Notes:
        Without the lock, concurrent names each built the 80K-row index,
        blocking the loop until the watchdog abandoned the pass.
    """
    async with _building():
        await asyncio.to_thread(index)
    return lookup_name(name)
