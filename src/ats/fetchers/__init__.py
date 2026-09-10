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
"""

from .adp_wfn import fetch_adp
from .api import fetch_ashby, fetch_greenhouse, fetch_lever
from .bamboohr import fetch_bamboohr
from .careeronestop import fetch_nlx_company
from .discourse import fetch_discourse
from .getro import fetch_getro_all
from .hibob import fetch_hibob
from .hnhiring import fetch_hnhiring
from .html_scrape import fetch_kula, fetch_successfactors
from .icims import fetch_icims
from .jazzhr import fetch_jazzhr
from .jobvite import fetch_jobvite
from .jsonld import fetch_jsonld_page
from .paylocity import fetch_paylocity
from .peopleadmin import fetch_peopleadmin
from .remoteok import fetch_remoteok
from .remotive import fetch_remotive
from .rippling import fetch_rippling
from .rssfeed import fetch_rss
from .ultipro import fetch_ultipro
from .usajobs import fetch_usajobs
from .websearch import fetch_websearch
from .workday import fetch_workday

__all__ = [
    "fetch_adp",
    "fetch_ashby",
    "fetch_bamboohr",
    "fetch_discourse",
    "fetch_getro_all",
    "fetch_greenhouse",
    "fetch_hibob",
    "fetch_hnhiring",
    "fetch_icims",
    "fetch_jazzhr",
    "fetch_jobvite",
    "fetch_jsonld_page",
    "fetch_kula",
    "fetch_lever",
    "fetch_nlx_company",
    "fetch_paylocity",
    "fetch_peopleadmin",
    "fetch_remoteok",
    "fetch_remotive",
    "fetch_rippling",
    "fetch_ultipro",
    "fetch_rss",
    "fetch_successfactors",
    "fetch_usajobs",
    "fetch_websearch",
    "fetch_workday",
]
