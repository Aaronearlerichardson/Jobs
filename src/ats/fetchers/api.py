"""ATS platforms with a clean public JSON API: Greenhouse, Lever, Ashby.

One GET returns the whole board with descriptions inline, so there is no
detail call and the rows go straight through `board.board_jobs`. A
posting's location is its primary field plus every secondary office or
location it names, joined: multi-location postings often show only
"Remote" (or one HQ city) up front while the site that matters hides in
the secondary list, and the location regex and the geo logic downstream
must see them all.
"""

import html
import re

from src.config import PROBE_TIMEOUT
from src.net.http import HEADERS, SESSION, get_json
from src.net.util import norm_posted_date, text_from_html
from .board import board_fetch

#: The public API root each platform answers on, and the Greenhouse job-page
#: URL shape. Public, and read from here rather than re-derived, because the
#: per-posting closure probe (fetchers/probe.py) and
#: ops.maintenance._live_jd address the same three APIs for one posting
#: instead of the whole board -- a second copy of a root or of the URL shape
#: is a silent "unverifiable", never an error.
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards"
LEVER_API = "https://api.lever.co/v0/postings"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board"
GREENHOUSE_JOB_URL_RE = re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?"
                                   r"([A-Za-z0-9_.-]+)/jobs/(\d+)")


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


# --------------------------------------------------------------------------- #
#  The cheap whole-board read: title + location, no descriptions              #
# --------------------------------------------------------------------------- #
#
# Shared by discovery's slug probe, NC count, and mission-context sample --
# one payload-shape reader so a private copy can't silently misread a key
# and return an empty board.

#: The whole-board URL per platform; `{}` takes the handle. Descriptions are
#: excluded where the API allows it -- none of the three callers reads one.
BOARD_URLS = {
    "greenhouse": GREENHOUSE_API + "/{}/jobs?content=false",
    "lever":      LEVER_API + "/{}?mode=json",
    "ashby":      ASHBY_API + "/{}",
}

#: Per platform: the posting list in the payload, then a posting's title and
#: its location.
_BOARD_SHAPE = {
    "greenhouse": (lambda d: d.get("jobs", []) if isinstance(d, dict) else [],
                   lambda j: j.get("title", ""),
                   lambda j: (j.get("location") or {}).get("name", "")),
    "lever":      (lambda d: d if isinstance(d, list) else [],
                   lambda j: j.get("text", ""),
                   lambda j: (j.get("categories") or {}).get("location", "")),
    # The posting API says "jobs"; only the embed payload says "jobPostings".
    "ashby":      (lambda d: d.get("jobs", d.get("jobPostings", []))
                   if isinstance(d, dict) else [],
                   lambda j: j.get("title", ""),
                   lambda j: j.get("location", "")),
}


def board_rows(ats, data):
    """The postings in a whole-board payload from `ats`.

    >>> board_rows("greenhouse", {"jobs": [{"title": "Scientist"}]})
    [{'title': 'Scientist'}]
    >>> board_rows("lever", [{"text": "Scientist"}])
    [{'text': 'Scientist'}]
    >>> board_rows("ashby", {"jobPostings": [{"title": "Scientist"}]})
    [{'title': 'Scientist'}]

    A payload of the wrong shape, and a platform with no whole-board JSON
    endpoint, are both "no postings" rather than an exception -- every
    caller is speculative and has to carry on:

    >>> board_rows("greenhouse", None)
    []
    >>> board_rows("workday", {"jobs": [{"title": "Scientist"}]})
    []
    """
    shape = _BOARD_SHAPE.get(ats)
    if not shape:
        return []
    try:
        return list(shape[0](data) or [])
    except Exception:
        return []


def board_summary(ats, handle, timeout=None):
    """``[(title, location), ...]`` for every posting on one board, or [] on
    any HTTP/JSON failure and on a platform outside BOARD_URLS.

    Quiet by design -- no `net.http.fetch_failed` line. Every caller reads a
    GUESSED handle (a slug probe, the locality count that rejects a
    collision, the mission scorer's title sample), so a miss is the expected
    answer rather than a dead source worth logging.
    """
    if ats not in BOARD_URLS:
        return []
    _rows, title_of, loc_of = _BOARD_SHAPE[ats]
    try:
        r = SESSION.get(BOARD_URLS[ats].format(handle),
                        timeout=timeout or PROBE_TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    return [(title_of(j) or "", loc_of(j) or "") for j in board_rows(ats, data)]


def _greenhouse_body(content):
    """Greenhouse `content` as readable text.

    Unescaped BEFORE the shared stripper runs: `content` is a JSON string
    holding "&lt;p&gt;...", so a stripper handed it directly finds no
    markup to strip and hands back the tags as TEXT.

    >>> _greenhouse_body("&lt;p&gt;Build EEG&amp;nbsp;pipelines&lt;/p&gt;")
    'Build EEG\\xa0pipelines'

    A board that answers with ordinary HTML is unharmed -- unescaping
    markup that is already markup changes nothing:

    >>> _greenhouse_body("<p>Build EEG pipelines</p>")
    'Build EEG pipelines'
    >>> _greenhouse_body("")
    ''

    Notes:
        Before this existed, 19,367 of 19,413 stored Greenhouse
        descriptions carried literal <div>/<p>/<li> markup in the body the
        keyword gates, the fit prompt and the digest all read (2026-09-22
        store audit).
    """
    return text_from_html(html.unescape(content or ""))


def _greenhouse_row(slug, j):
    title = j.get("title", "")
    loc = merge_locations((j.get("location") or {}).get("name", ""),
                          [o.get("name", "") for o in (j.get("offices") or [])])
    dept = " ".join(d.get("name", "") for d in j.get("departments", []) or [])
    offices = " ".join((o.get("name") or "") for o in j.get("offices", []) or [])
    row = {"id": f"gh_{slug}_{j.get('id', '')}", "title": title,
           "url": j.get("absolute_url", ""), "location": loc or "Unknown",
           "description": _greenhouse_body(j.get("content", "") or ""),
           "posted_at": norm_posted_date(j.get("first_published")
                                         or j.get("updated_at")),
           "head": f"{title} {dept}"}
    if "remote" in offices.lower():
        row["remote_hint"] = "greenhouse:office"
    return row


def fetch_greenhouse(slug, company_name="", gate=None, loc_re=None):
    return _fetch_board("greenhouse", slug, company_name, gate, loc_re)


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
    return _fetch_board("lever", slug, company_name, gate, loc_re)


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
    return _fetch_board("ashby", slug, company_name, gate, loc_re)


# ── The one whole-board crawl, three platforms ──────────────────────────── #
#
# The three fetchers above each kept a SECOND, private copy of its payload's
# shape, a few lines below the `_BOARD_SHAPE` table that already holds it for
# the summary path -- and a copy of a shape is a silently empty board, never
# an error. (One such copy once asked for Workday's "jobPostings" key and
# zeroed every Ashby board; pinned now by
# tests/test_fetcher_parsers.py::TestAshby and the board canary,
# tools/check_boards.py.) `board_rows` is the one read of the shape for both
# callers, and it subsumes the isinstance guards: a wrong top-level type, and
# the None _get_board returns on a failed response, are both "no postings".

#: Per platform: the whole-board URL WITH descriptions (`{}` takes the slug
#: -- this is the crawl, not the metadata-only read BOARD_URLS serves), the
#: name used in the failure line, and the row builder.
_FETCH = {
    "greenhouse": (GREENHOUSE_API + "/{}/jobs?content=true", "Greenhouse",
                   _greenhouse_row),
    "lever":      (LEVER_API + "/{}?mode=json", "Lever", _lever_row),
    "ashby":      (ASHBY_API + "/{}", "Ashby", _ashby_row),
}


def _fetch_board(ats, slug, company_name, gate, loc_re):
    url, platform, row = _FETCH[ats]
    label = f"{platform} {company_name or slug}"
    return board_fetch(
        label,
        lambda: board_rows(ats, _get_board(url.format(slug), label)),
        lambda j: row(slug, j),
        company_name, gate=gate, loc_re=loc_re)
