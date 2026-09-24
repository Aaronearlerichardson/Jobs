"""
Company-scoped fetching: ALL of a *mission-vetted* company's postings,
optionally location-filtered, in one shape whatever the ATS; and the
readers for one stored posting.

The company was already vetted (mission scored at discovery time, stored in
src/store/__init__.py), so the whole board is pulled with no relevance gate and the
caller's own filter chain decides. `fetch_company` dispatches a store row
to its platform's engine, `board.Board.whole_board`, which reads the
`config.BOARDS` spec:

    {"id", "title", "url", "location", "description", "ats",
     ["posted_at"], ["remote_hint"]}

`hydrate_description` fills one stored posting through the engine that
reads its URL, else from the posting's own page (`job_page_meta`). The
per-job open/closed probe is board/closure.py; the careers-page reader
behind the `custom` spec is board/custom.py.
"""

import re
from urllib.parse import unquote

from bs4 import BeautifulSoup

from src import config
from src.match.locality import NC_RE  # profile [locality]
from src.net.http import HEADERS, PLAIN_HEADERS, SESSION
from src.net.util import clean_field
from .engine import board_for, board_for_url
from . import jsonld

# JD text budget (config.MAX_DESC_CHARS): one cap shared with storage and the
# scoring prompt, so a long posting's requirements block survives end to end.
_DESC_MAX = config.MAX_DESC_CHARS


def _board_of(job):
    """The engine that reads `job`'s posting: its ATS's, when that one
    reads the URL; else the one whose `job_ref` does; else its ATS's."""
    board = board_for(job.get("ats"))
    if board and board.owns_url(job.get("url")):
        return board
    return board_for_url(job.get("url")) or board


def needs_detail(job):
    """True when hydrate_description would fetch anything for `job`: no
    body yet, or a body already but a location the listing never resolved
    that the posting's engine can fill (`Board.needs_detail`). Shared by
    harvest._hydrate_rows and triage._hydrate, which both select rows to
    fetch by this predicate rather than "no description" alone.

    >>> needs_detail({"description": "", "ats": "greenhouse"})
    True
    >>> needs_detail({"description": "d", "ats": "greenhouse"})
    False
    >>> url = "https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/x/Eng_R1"
    >>> needs_detail({"description": "d", "ats": "workday", "url": url,
    ...               "location": "2 Locations"})
    True
    >>> needs_detail({"description": "d", "ats": "workday", "url": url,
    ...               "location": "Durham, NC"})
    False
    """
    board = _board_of(job)
    return board.needs_detail(job) if board else not job.get("description")


def hydrate_description(job, company=None):
    """Fetch, in place, whatever `needs_detail` says `job` still lacks,
    through the posting's engine (`Board.hydrate`; `company`, the row's
    store row, names its board), else from the posting's own page.
    """
    if not needs_detail(job):
        return job
    board = _board_of(job)
    if board:
        board.hydrate(job, company)
    # A posting no engine gave a body: its own page (a custom board's, a
    # SuccessFactors site's).
    if not job.get("description") and job.get("url"):
        d = _description_from_job_url(job["url"])
        if d:
            job["description"] = d
    return job


def _description_from_job_url(url):
    """Best-effort JD text from a job's own detail page (see job_page_meta).
    Returns '' on miss."""
    return job_page_meta(url)[1]


def job_page_meta(url):
    """(title, description) read off a job's own detail page, vendor-
    agnostically: schema.org JSON-LD JobPosting first (hundreds of sites),
    then page metadata for the title (og:title, then <title> minus a
    " | site" suffix) and SuccessFactors Career-Site-Builder markup for the
    description (data-careersite-propertyid='description', the SAP SF
    frontends). Either field is '' on a miss. The title half exists for
    URL-only manual adds, which otherwise stored an empty title that
    nothing downstream could score or rank. A page refused with 403/405 is
    asked again with a bare platform UA (`PLAIN_HEADERS`), which WAFs that
    refuse a Chrome UA without Chrome's client hints accept."""
    try:
        r = SESSION.get(url, headers=HEADERS,
                        allow_redirects=True)
        if r.status_code in (403, 405):
            r = SESSION.get(url, allow_redirects=True, headers=PLAIN_HEADERS)
        html = r.text
    except Exception:
        return "", ""
    title = desc = ""
    try:
        p = next((jsonld.read_posting(o, url) for o in jsonld.extract_jsonld(html)
                  if jsonld.is_jobposting(o)), None)
        if p:
            title = p["title"]
            d = p["description"].strip()
            if len(d) >= 120:
                desc = d[:_DESC_MAX]
    except Exception:
        pass
    if title and desc:
        return title, desc
    try:
        soup = BeautifulSoup(html, "lxml")
        if not title:
            og = soup.find("meta", attrs={"property": "og:title"})
            raw = (og.get("content") if og else "") or \
                (soup.title.get_text(" ") if soup.title else "")
            title = re.sub(r"\s+", " ", raw or "").split(" | ")[0].strip()
        if not desc:
            el = (soup.select_one('[data-careersite-propertyid="description"]')
                  or soup.select_one('[data-careersite-propertyid="jobdescription"]')
                  # Custom boards that name the JD container ("_flow
                  # job-description"). Kept specific ('job-description'/
                  # 'jobDescription', not a bare 'description') so a short
                  # company tagline can't match.
                  or soup.select_one('[class*="job-description"]')
                  or soup.select_one('[class*="jobDescription"]')
                  # SuccessFactors' CLASSIC (pre-Career-Site-Builder) template
                  # wraps the posting in .jobDisplay. Last in the chain: it
                  # carries a little page chrome, so the precise containers win.
                  or soup.select_one('[class*="jobDisplay"]'))
            if el:
                d = el.get_text(" ", strip=True)
                if len(d) >= 120:
                    desc = d[:_DESC_MAX]
    except Exception:
        pass
    return title, desc


def title_from_url_slug(url):
    """Last-resort title for a URL-only manual add: the path segment with
    the most word tokens, digits and separators normalized. Two words
    minimum, so an id-only path yields '' rather than nonsense.

    >>> title_from_url_slug("https://careers.example.com/details/173531/sr_vision_software_engineer#apply")
    'Sr Vision Software Engineer'
    >>> title_from_url_slug("https://x.icims.com/jobs/42453/software-developer/job?in_iframe=1")
    'Software Developer'
    >>> title_from_url_slug("https://x.com/jobs/1970393556937343")
    ''
    """
    path = re.sub(r"[?#].*$", "", url or "")
    path = re.sub(r"^https?://[^/]+", "", path)
    best = []
    for seg in path.split("/"):
        seg = unquote(seg)
        if re.search(r"\.[a-z]{2,5}$", seg, re.I):     # a file, not a slug
            continue
        words = [w for w in re.split(r"[-_+\s]+", seg)
                 if re.search(r"[A-Za-z]", w)]
        if len(words) > len(best):
            best = words
    if len(best) < 2:
        return ""
    return " ".join(w[:1].upper() + w[1:] for w in best)


# --- dispatch ------------------------------------------------------------------ #

def fetch_company(company, loc_re=None):
    """A store row's board pulled through its platform's engine
    (`Board.whole_board`); [] for a platform no spec fetches.

    `loc_re=None` pulls the whole board; pass NC_RE for a pull scoped to
    the profile's locality (the local track's default).
    """
    board = board_for(company.get("ats"))
    return board.whole_board(company, loc_re) if board else []


# fetch_company with the profile's locality regex; used by discovery
# (ats_dork, local_sourcing) to sample a board's local postings.
def fetch_company_nc(company):
    return fetch_company(company, NC_RE)


# --- title sampling ------------------------------------------------------------ #

def sample_titles(company, n=6):
    """Up to `n` distinct posting titles from a store row's board, in board
    order: what the mission scorer is shown of an employer it has only a
    name for. [] when the board is unreadable, empty, or of an ATS with no
    engine; never raises.

    `company` carries the store's board columns (`src.ats.coords.columns`;
    `from_hit` turns a resolver hit into them). The pull is listing-only: no
    description, detail or location-rescue request is spent on a sample,
    except where the posting pages are the listing (at most `n` of them).

    Notes:
        Until 2026-09-18 the sampler lived in src.discovery.local_sourcing
        with hand-written requests for four ATS families and fell through to
        [] for the other sixteen, 35% of the roster's boards: a company on
        one was mission-scored from its name alone. "Studycast" (Rippling
        board core-sound-imaging, a medical-imaging vendor's PACS product)
        came back `other` / 0.05 as a study-education platform.
    """
    board = board_for(company.get("ats"))
    try:
        handle = board.handle(company) if board else None
        jobs = board.listing(handle, cheap=True, rescue_cap=n) if handle else []
    except Exception:
        return []
    titles, seen = [], set()
    for j in jobs:
        title = clean_field(j.get("title"))
        if title and title.lower() not in seen:
            seen.add(title.lower())
            titles.append(title)
            if len(titles) >= n:
                break
    return titles
