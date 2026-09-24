"""Feed fetchers: the job sources that are not ATS boards (forums,
aggregator feeds, USAJOBS, Getro networks, web search, CareerOneStop).

Each fetcher returns a list of job dicts with the shape:
    {"id", "company", "title", "url", "location", "description"}
plus `posted_at` / `remote_hint` where the source supplies them.

A fetcher's `gate=None` is a relevance predicate
`gate(title[, description])`; None keeps every posting.

Board-shaped platforms are `config.BOARDS` specs run by the engine in
src/ats/board/.
"""

from .careeronestop import fetch_nlx_company
from .discourse import fetch_discourse
from .getro import fetch_getro_all
from .hnhiring import fetch_hnhiring
from .remoteok import fetch_remoteok
from .remotive import fetch_remotive
from .rssfeed import fetch_rss
from .usajobs import fetch_usajobs
from .websearch import fetch_websearch

__all__ = [
    "fetch_discourse",
    "fetch_getro_all",
    "fetch_hnhiring",
    "fetch_nlx_company",
    "fetch_remoteok",
    "fetch_remotive",
    "fetch_rss",
    "fetch_usajobs",
    "fetch_websearch",
]
