"""Phenom People career sites: hospital-system and enterprise boards built
on the Phenom "CareerConnect" platform (careers.dukehealth.org and
similar). No public JSON API answers a plain out-of-session request; every
page -- the listing at ``/search-results`` and a posting's own page at
``/job/<reqId>`` -- instead embeds its own data as a ``phApp.ddo = {...}``
JSON blob in a ``<script>`` tag, which is what this fetcher reads.

Two things about the platform bit a naive scrape:

* A bare path like ``/search-results`` 404s into the tenant's localized
  homepage (``/us/en``), silently dropping any query string. Every board
  is served under a locale prefix this module cannot guess (``/us/en``
  for Duke Health, potentially different elsewhere), so every call first
  resolves it by following the root URL's redirect (`_locale_base`).
* The listing's row ORDER IS UNSTABLE between requests for the same
  range (server-side, unconfirmed cause -- a multi-shard index with tied
  relevance scores is the likely one). Naive sequential paging (0, 500,
  1000, ...) silently lost ~5% of a 906-job board's rows in the first
  cut of this fetcher (2026-08). `fetch_phenom_all` pages with HALF-PAGE
  overlap instead and dedupes by job id, so a row that shifts across the
  boundary between two requests is still caught by the next one.

The server caps ``size`` at 500 regardless of what is asked for.
"""

import json
import re

from src.net.http import HEADERS, SESSION, fetch_failed, note_capped
from src.net.util import norm_posted_date, text_from_html
from .board import board_jobs, loc_ok

_PAGE_SIZE = 500                 # server-enforced cap on `size`
_MAX_PAGES = 40                  # safety valve; real boards stop long before this

_DDO_RE = re.compile(r"phApp\.ddo\s*=\s*")
# This module's own job URLs only ("<base>/job/<reqId>"); base is
# everything up to the LAST "/job/" segment, which is why the capture is
# greedy rather than restricted to a fixed locale-prefix shape.
_JOB_URL_RE = re.compile(r"^(https?://.+)/job/([^/?#]+)/?$", re.I)


def _parse_ddo(html_text):
    """The `phApp.ddo` JSON blob embedded in a Phenom page, or None.

    `json.JSONDecoder.raw_decode` reads exactly one JSON value starting at
    the given offset and ignores whatever trailing script/markup follows
    it (the ``;`` and the rest of the page) -- simpler and more robust
    than counting braces by hand.
    """
    m = _DDO_RE.search(html_text or "")
    if not m:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(html_text, m.end())
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _locale_base(host_or_url):
    """The locale-prefixed base URL a board's bare host resolves to, e.g.
    "careers.dukehealth.org" -> "https://careers.dukehealth.org/us/en".

    A path guessed without this (`/search-results` off the bare host)
    404s into the localized homepage and drops any query string, so every
    other call in this module resolves it here first. None on failure.
    """
    url = host_or_url if re.match(r"^https?://", host_or_url or "", re.I) \
        else f"https://{host_or_url}"
    try:
        r = SESSION.get(url, headers=HEADERS)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    return re.sub(r"[?#].*$", "", r.url or url).rstrip("/") or None


def job_ref_from_url(url):
    """(base, reqId) from one of this module's own job URLs, or None.
    Lets `fetchers/company.py`'s hydrate_description re-derive detail
    coordinates from a stored job's `url` alone -- the company-vetted
    path's `_adapt` keeps no ATS-specific "_"-prefixed key besides
    Workday's `_wd`, so a URL-only round trip is what every other
    ATS-specific hydrate branch does too (infor, icims).

    >>> job_ref_from_url("https://careers.dukehealth.org/us/en/job/273419")
    ('https://careers.dukehealth.org/us/en', '273419')
    >>> job_ref_from_url("not a url") is None
    True
    """
    m = _JOB_URL_RE.match(url or "")
    return (m.group(1), m.group(2)) if m else None


def _job_location(j):
    if j.get("location"):
        return j["location"]
    if j.get("cityStateCountry"):
        return j["cityStateCountry"]
    if j.get("cityState"):
        return j["cityState"]
    bits = [x for x in (j.get("city"), j.get("state"), j.get("country")) if x]
    return ", ".join(bits)


def _job_row(base, j):
    req_id = str(j.get("reqId") or j.get("jobId") or "")
    title = (j.get("title") or "").strip()
    if not req_id or not title:
        return None
    return {"id": f"phenom_{req_id}", "title": title,
            "url": f"{base}/job/{req_id}", "location": _job_location(j),
            "description": "", "posted_at": norm_posted_date(j.get("postedDate")),
            "_phenom": (base, req_id)}


def fetch_phenom_all(host_or_url, loc_re=None, page_size=_PAGE_SIZE,
                     max_pages=_MAX_PAGES):
    """List every posting on a Phenom board (title/location/reqId only;
    descriptions hydrate lazily -- see `fetchers.company.hydrate_description`).

    `loc_re`, when given, is a POST-filter over each row's listed
    location: the refineSearch listing has no server-side locality scope
    this fetcher can drive from outside the page (unlike Workday's facet
    search), so a location-scoped pull still reads the whole board.

    Pages with half-page overlap and dedupes by job id (see module doc
    for why); stops once a page adds no new id, or the server's own
    `totalHits` is reached, whichever comes first. Stopping short of
    `totalHits`, or reading all `max_pages`, reports a capped snapshot
    (net.http.note_capped): the unstable order that repeats a page is
    exactly what leaves rows unseen.
    """
    base = _locale_base(host_or_url)
    if not base:
        return fetch_failed(f"phenom {host_or_url}", "could not resolve the board's locale path")

    step = max(page_size // 2, 1)    # half-page overlap (see module doc)
    seen, out, total, frm = set(), [], None, 0
    for _ in range(max_pages):
        try:
            r = SESSION.get(f"{base}/search-results",
                            params={"from": frm, "size": page_size}, headers=HEADERS)
            if r.status_code != 200:
                raise ValueError(f"HTTP {r.status_code}")
            ers = (_parse_ddo(r.text) or {}).get("eagerLoadRefineSearch") or {}
            jobs = ((ers.get("data") or {}).get("jobs")) or []
        except Exception as e:
            fetch_failed(f"phenom {host_or_url} from={frm}", e)
            break
        if total is None and isinstance(ers.get("totalHits"), (int, float)):
            total = ers["totalHits"]
        new_ids = 0
        for j in jobs:
            row = _job_row(base, j)
            if not row or row["id"] in seen:
                continue
            seen.add(row["id"])
            new_ids += 1
            if loc_ok(loc_re, row["location"]):
                out.append(row)
        if not jobs or (new_ids == 0 and frm > 0):
            break
        if total is not None and len(seen) >= total:
            break
        frm += step
    else:
        note_capped(total)
    if total is not None and len(seen) < total:
        note_capped(total)
    return out


def detail(base, req_id):
    """The `jobDetail.data.job` object for one posting, or {} on failure."""
    try:
        r = SESSION.get(f"{base}/job/{req_id}", headers=HEADERS)
        if r.status_code != 200:
            return {}
        ddo = _parse_ddo(r.text) or {}
        return ((ddo.get("jobDetail") or {}).get("data") or {}).get("job") or {}
    except Exception:
        return {}


def detail_description(job_detail):
    return text_from_html(job_detail.get("description") or "")


def detail_location(job_detail):
    loc = _job_location(job_detail)
    if loc:
        return loc
    locs = [l.get("standardisedMapQueryLocation") for l in
            job_detail.get("standardised_multi_location") or []
            if l.get("standardisedMapQueryLocation")]
    return "; ".join(locs)


def fetch_phenom(host_or_url, company_name="", gate=None, loc_re=None,
                 max_details=40, detail_delay=0.2):
    """`fetch_phenom_all`'s rows through the board driver (fetchers/board.py):
    `gate` screens titles and then, within `max_details` detail-page GETs,
    descriptions."""
    rows = fetch_phenom_all(host_or_url, loc_re=loc_re)
    return board_jobs(
        rows, company_name, gate=gate,
        fetch_description=lambda row: detail_description(detail(*row["_phenom"])),
        max_details=max_details, detail_delay=detail_delay)
