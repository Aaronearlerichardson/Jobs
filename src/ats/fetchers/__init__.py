"""Per-source job fetchers.

Each fetcher returns a list of job dicts with the shape:
    {"id", "company", "title", "url", "location", "description"}
plus `posted_at` / `remote_hint` where the source supplies them.

The board-shaped ATS fetchers take `gate=None` (a relevance predicate
`gate(title[, description])`; None keeps every posting) and `loc_re=None`
(a location regex applied to the listed location before any detail call);
see board.py for the order of filters. The registry in src/ats/registry.py
passes the profile's keyword gate for the unvetted-board sweep; the
company-vetted path (company.py) passes a location regex instead.

Platforms with a `config.BOARDS` spec have no fetch function here: they
run on the engine in board.py (`board.board_for(ats).jobs(...)`).
"""

from .careeronestop import fetch_nlx_company
from .discourse import fetch_discourse
from .getro import fetch_getro_all
from .hnhiring import fetch_hnhiring
from .html_scrape import fetch_kula, fetch_successfactors
from .icims import fetch_icims
from .jazzhr import fetch_jazzhr
from .jobvite import fetch_jobvite
from .jsonld import fetch_jsonld_page
from .peopleadmin import fetch_peopleadmin
from .remoteok import fetch_remoteok
from .remotive import fetch_remotive
from .rssfeed import fetch_rss
from .usajobs import fetch_usajobs
from .websearch import fetch_websearch

__all__ = [
    "fetch_discourse",
    "fetch_getro_all",
    "fetch_hnhiring",
    "fetch_icims",
    "fetch_jazzhr",
    "fetch_jobvite",
    "fetch_jsonld_page",
    "fetch_kula",
    "fetch_nlx_company",
    "fetch_peopleadmin",
    "fetch_remoteok",
    "fetch_remotive",
    "fetch_rss",
    "fetch_successfactors",
    "fetch_usajobs",
    "fetch_websearch",
]
