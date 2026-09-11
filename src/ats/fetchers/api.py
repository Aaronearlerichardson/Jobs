"""ATS platforms with a clean public JSON API: Greenhouse, Lever, Ashby.

One GET returns the whole board with descriptions inline, so there is no
detail call and the rows go straight through `board.board_jobs`. A
posting's location is its primary field plus every secondary office or
location it names, joined: multi-location postings often show only
"Remote" (or one HQ city) up front while the site that matters hides in
the secondary list, and the location regex and the geo logic downstream
must see them all.
"""

from bs4 import BeautifulSoup

from src.net.http import SESSION, HEADERS, get_json
from src.net.util import norm_posted_date
from .board import board_jobs


def merge_locations(primary, extras):
    """One location string carrying every location a posting names: the
    primary field first, then any secondary office/location not already
    present in it.

    >>> merge_locations("Remote", ["Durham, NC", "Remote"])
    'Remote; Durham, NC'
    >>> merge_locations("", ["Tokyo, Japan"])
    'Tokyo, Japan'
    >>> merge_locations(None, [])
    ''
    """
    loc = (primary or "").strip()
    seen = loc.lower()
    for e in extras or []:
        e = (e or "").strip()
        if e and e.lower() not in seen:
            loc = f"{loc}; {e}" if loc else e
            seen = loc.lower()
    return loc


#: The board's JSON, or None (reported) on any HTTP or JSON failure.
#: net.http.get_json -- eight fetchers had written this out.
_get_board = get_json


def _greenhouse_row(slug, j):
    title = j.get("title", "")
    loc = merge_locations((j.get("location") or {}).get("name", ""),
                          [o.get("name", "") for o in (j.get("offices") or [])])
    dept = " ".join(d.get("name", "") for d in j.get("departments", []) or [])
    offices = " ".join((o.get("name") or "") for o in j.get("offices", []) or [])
    row = {"id": f"gh_{slug}_{j.get('id', '')}", "title": title,
           "url": j.get("absolute_url", ""), "location": loc or "Unknown",
           "description": BeautifulSoup(j.get("content", "") or "",
                                        "html.parser").get_text(" "),
           "posted_at": norm_posted_date(j.get("first_published")
                                         or j.get("updated_at")),
           "head": f"{title} {dept}"}
    if "remote" in offices.lower():
        row["remote_hint"] = "greenhouse:office"
    return row


def fetch_greenhouse(slug, company_name="", gate=None, loc_re=None):
    data = _get_board(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                      f"Greenhouse {company_name or slug}")
    if not isinstance(data, dict):
        return []
    return board_jobs((_greenhouse_row(slug, j) for j in data.get("jobs", [])),
                      company_name, gate=gate, loc_re=loc_re)


def _lever_row(slug, j):
    title = j.get("text", "")
    cats = j.get("categories", {}) or {}
    loc = merge_locations(cats.get("location", ""),
                          cats.get("allLocations") or j.get("allLocations") or [])
    row = {"id": f"lv_{slug}_{j.get('id', '')}", "title": title,
           "url": j.get("hostedUrl", ""), "location": loc or "Unknown",
           "description": j.get("descriptionPlain") or "",
           "posted_at": norm_posted_date(j.get("createdAt")),
           "head": f"{title} {cats.get('team', '')}"}
    if str(j.get("workplaceType", "")).lower() == "remote":
        row["remote_hint"] = "lever:workplaceType"
    return row


def fetch_lever(slug, company_name="", gate=None, loc_re=None):
    data = _get_board(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                      f"Lever {company_name or slug}")
    if not isinstance(data, list):
        return []
    return board_jobs((_lever_row(slug, j) for j in data),
                      company_name, gate=gate, loc_re=loc_re)


def _ashby_row(slug, j):
    title = j.get("title", "")
    jid = j.get("id", "")
    secondary = [s.get("location", "") if isinstance(s, dict) else str(s)
                 for s in (j.get("secondaryLocations") or [])]
    loc = merge_locations(j.get("location", "") or "", secondary)
    dept = " ".join(x for x in (j.get("department"), j.get("team")) if x)
    row = {"id": f"ashby_{slug}_{jid}", "title": title,
           "url": j.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{jid}",
           "location": loc or "Unknown",
           "description": j.get("descriptionPlain", "") or "",
           "posted_at": norm_posted_date(j.get("publishedDate")
                                         or j.get("publishedAt")),
           "head": f"{title} {dept}"}
    # workplaceType is the structured signal; isRemote is the older
    # boolean. Either one beats regexing the location string.
    if j.get("isRemote") is True or j.get("workplaceType") == "Remote":
        row["remote_hint"] = "ashby:isRemote"
    return row


def fetch_ashby(slug, company_name="", gate=None, loc_re=None):
    data = _get_board(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                      f"Ashby {company_name or slug}")
    if not isinstance(data, dict):
        return []
    # The posting-api returns {"jobs": [...], "apiVersion": ...}. A copy of
    # this read once asked for "jobPostings" (Workday's key), so EVERY Ashby
    # board silently yielded zero postings: a missing key is an empty list,
    # and the caller can't tell that from "no matches". Pinned by
    # tests/test_fetcher_parsers.py::TestAshby and the board canary
    # (tools/check_boards.py).
    return board_jobs((_ashby_row(slug, j) for j in data.get("jobs", [])),
                      company_name, gate=gate, loc_re=loc_re)
