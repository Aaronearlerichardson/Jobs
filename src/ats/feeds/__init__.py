"""Feed fetchers: the job sources that are not ATS boards (forums,
aggregator feeds, USAJOBS, Getro networks, CareerOneStop).

Each fetcher returns a list of job dicts with the shape:
    {"id", "company", "title", "url", "location", "description"}
plus `posted_at` / `remote_hint` where the source supplies them.

A fetcher keeps every posting: relevance is triage's call, not the fetch's.

Each fetcher is a coroutine, `fetch_<name>`.

Board-shaped platforms are `config.BOARDS` specs run by the engine in
src/ats/board/.

FETCH maps each feed family of `config.FEEDS` to its fetcher:

    >>> from src import config
    >>> FETCH.keys() == config.FEEDS.keys()
    True
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from src.rows import FetchedJob

from .careeronestop import fetch_nlx_company
from .discourse import fetch_discourse
from .getro import fetch_getro_all
from .hnhiring import fetch_hnhiring
from .remoteok import fetch_remoteok
from .remotive import fetch_remotive
from .rssfeed import fetch_rss
from .usajobs import fetch_usajobs

FETCH: dict[str, Callable[..., Awaitable[list[FetchedJob]]]] = {
    "remoteok": fetch_remoteok, "remotive": fetch_remotive, "hn": fetch_hnhiring,
    "rss": fetch_rss, "discourse": fetch_discourse, "usajobs": fetch_usajobs,
    "getro": fetch_getro_all,
}

__all__ = [
    "FETCH",
    "fetch_discourse",
    "fetch_getro_all",
    "fetch_hnhiring",
    "fetch_nlx_company",
    "fetch_remoteok",
    "fetch_remotive",
    "fetch_rss",
    "fetch_usajobs",
]
