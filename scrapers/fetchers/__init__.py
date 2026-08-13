"""Per-source job fetchers.

Each fetcher returns a list of job dicts with the shape:
    {"id", "company", "title", "url", "location", "description"}
"""

from .adp_wfn import fetch_adp
from .ats_api import fetch_ashby, fetch_greenhouse, fetch_lever
from .bamboohr import fetch_bamboohr
from .careeronestop import fetch_nlx_company
from .discourse import fetch_discourse
from .hnhiring import fetch_hnhiring
from .html_scrape import fetch_custom, fetch_kula, fetch_successfactors
from .jazzhr import fetch_jazzhr
from .jsonld import fetch_jsonld_careers, fetch_jsonld_page
from .paylocity import fetch_paylocity
from .peopleadmin import fetch_peopleadmin
from .remoteok import fetch_remoteok
from .remotive import fetch_remotive
from .rippling import fetch_rippling
from .rssfeed import fetch_rss
from .sitemap import fetch_sitemap
from .ultipro import fetch_ultipro
from .websearch import fetch_websearch
from .workday import fetch_workday

__all__ = [
    "fetch_adp",
    "fetch_ashby",
    "fetch_bamboohr",
    "fetch_custom",
    "fetch_discourse",
    "fetch_greenhouse",
    "fetch_hnhiring",
    "fetch_jazzhr",
    "fetch_jsonld_careers",
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
    "fetch_sitemap",
    "fetch_successfactors",
    "fetch_websearch",
    "fetch_workday",
]
