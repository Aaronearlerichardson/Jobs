"""Per-source job fetchers.

Each fetcher returns a list of job dicts with the shape:
    {"id", "company", "title", "url", "location", "description"}
"""

from .ats_api import fetch_ashby, fetch_greenhouse, fetch_lever
from .discourse import fetch_discourse
from .hnhiring import fetch_hnhiring
from .html_scrape import fetch_custom, fetch_kula, fetch_successfactors
from .jazzhr import fetch_jazzhr
from .jsonld import fetch_jsonld_careers, fetch_jsonld_page
from .peopleadmin import fetch_peopleadmin
from .remoteok import fetch_remoteok
from .remotive import fetch_remotive
from .rssfeed import fetch_rss
from .sitemap import fetch_sitemap
from .websearch import fetch_websearch
from .workday import fetch_workday

__all__ = [
    "fetch_ashby",
    "fetch_custom",
    "fetch_discourse",
    "fetch_greenhouse",
    "fetch_hnhiring",
    "fetch_jazzhr",
    "fetch_jsonld_careers",
    "fetch_jsonld_page",
    "fetch_kula",
    "fetch_lever",
    "fetch_peopleadmin",
    "fetch_remoteok",
    "fetch_remotive",
    "fetch_rss",
    "fetch_sitemap",
    "fetch_successfactors",
    "fetch_websearch",
    "fetch_workday",
]
