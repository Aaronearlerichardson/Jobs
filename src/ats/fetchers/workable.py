"""Workable public board fetcher.

Workable-hosted boards live at ``apply.workable.com/<slug>/`` -- a client-
rendered SPA -- but the widget the platform offers employers for embedding
their openings is backed by a clean public JSON API, anonymous and
unthrottled:

    GET  apply.workable.com/api/v1/widget/accounts/<slug>
    -> {"name", "description", "jobs": [{"title", "shortcode", "url",
        "department", "employment_type", "telecommuting", "published_on",
        "country", "city", "state", "locations": [...], ...}]}

    GET  apply.workable.com/api/v1/accounts/<slug>/jobs/<shortcode>
    -> {"title", "state", "remote", "workplace", "location", "locations",
        "description", "requirements", "benefits", ...}

Three things about the platform are worth knowing before reading the code:

* THE LISTING CARRIES NO DESCRIPTION, so a body costs one detail GET per
  posting (like Rippling and Paylocity, unlike Greenhouse/Lever/Ashby).
  ``?details=true`` on the widget endpoint is accepted and ignored --
  verified live 2026-09-21, byte-identical payload.
* THE LISTING IS THE WHOLE BOARD. The payload has exactly three keys and
  carries no cursor, count or ``nextPage``; ``limit``/``offset`` are
  ignored. There is nothing to page, so unlike the cursor-walking fetchers
  this one makes exactly one listing request and never reports a capped
  snapshot.
* A POSTING IS ADDRESSED BY ITS SHORTCODE, and the shortcode alone does not
  name a board: the detail endpoint wants ``<slug>/jobs/<shortcode>``. The
  listing's own ``url`` field is the slug-less short link
  (``apply.workable.com/j/<shortcode>``), which redirects to the tenant-path
  page but tells a later reader nothing about which account served it, so
  `job_url` mints the tenant-path form instead. That form is also what
  ``store.company_by_host`` needs to attribute a captured page to the right
  employer on this shared host, and what `job_ref_from_url` reads back so
  `fetchers/company.py`'s hydrate branch can re-derive both coordinates
  from a stored row's URL alone.

The store slug is the account slug (e.g. ``eupry-aps`` for Eupry, a Danish
cold-chain monitoring company with a Raleigh NC office). Note that it is
not always the company name: ``eupry`` is a DIFFERENT, empty Workable
account, so the slug has to come from the board URL, never from the name.

Replaces the detection-only "lead" treatment of Workable boards
(src.ats.signatures.ATS_LEAD_PATTERNS), which recognised the platform but
could fetch nothing from it.
"""

import re

from src.net.http import JSON_HEADERS, SESSION
from src.net.util import norm_posted_date
from .api import merge_locations
from .board import board_fetch
from .workday import text_from_html  # shared HTML->text stripper

_WIDGET_API = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
_JOB_API = "https://apply.workable.com/api/v1/accounts/{slug}/jobs/{shortcode}"

#: The tenant-path posting page -- this module's own URL shape. Public
#: because `fetchers/company.py`'s hydrate branch reads a stored row's URL
#: back through it (see `job_ref_from_url`), the way the infor and phenom
#: branches do, and a second copy of the shape is a silent miss, never an
#: error. The slug-less short link form (/j/<shortcode>) is deliberately
#: NOT matched: it names no account, so nothing can be fetched from it.
JOB_URL_RE = re.compile(
    r"^https?://apply\.workable\.com/([A-Za-z0-9][A-Za-z0-9_-]*)/j/([A-Za-z0-9]+)",
    re.I)


def board_url(slug):
    """The board page a person can open.

    >>> board_url("eupry-aps")
    'https://apply.workable.com/eupry-aps/'
    """
    return f"https://apply.workable.com/{slug}/"


def job_url(slug, shortcode):
    """The posting page a person can open, in the tenant-path form (see
    the module doc for why not the listing's own short link).

    >>> job_url("eupry-aps", "D68529D654")
    'https://apply.workable.com/eupry-aps/j/D68529D654/'
    """
    return f"https://apply.workable.com/{slug}/j/{shortcode}/"


def job_ref_from_url(url):
    """(slug, shortcode) from one of this module's own job URLs, or None.
    Lets a caller holding nothing but a stored job's `url` re-derive the
    detail coordinates -- `fetchers/company.py`'s `_adapt` keeps no
    ATS-specific key.

    >>> job_ref_from_url(job_url("eupry-aps", "D68529D654"))
    ('eupry-aps', 'D68529D654')

    The short link names no account, so it is not a reference:

    >>> job_ref_from_url("https://apply.workable.com/j/D68529D654") is None
    True
    >>> job_ref_from_url("https://example.org/jobs/1") is None
    True
    """
    m = JOB_URL_RE.match(url or "")
    return (m.group(1), m.group(2)) if m else None


def parse_board(slug, timeout=None):
    """The raw ``jobs`` list for one account slug (the whole board -- there
    is no paging). Raises on any HTTP or JSON failure, so the discovery
    probe (`src.discovery.resolve.probes.probe_workable`) and `fetch_workable`
    can each report it their own way.

    A slug that names no account 404s; an account with nothing published
    answers 200 with an empty list, which is a real empty board and not an
    error.
    """
    r = SESSION.get(_WIDGET_API.format(slug=slug), timeout=timeout,
                    headers=JSON_HEADERS)
    r.raise_for_status()
    data = r.json()
    return (data.get("jobs") or []) if isinstance(data, dict) else []


def _place(loc):
    """One ``locations[]`` entry as "City, Region, Country", broadest last.

    >>> _place({"city": "Raleigh", "region": "North Carolina",
    ...         "country": "United States"})
    'Raleigh, North Carolina, United States'

    Workable spells the region out ("North Carolina", not "NC") and lets a
    tenant leave any level blank, so every partial form has to stay
    readable -- and keep the city adjacent to whatever names its state,
    which is the one property `match.locality` depends on:

    >>> _place({"city": "Copenhagen", "country": "Denmark"})
    'Copenhagen, Denmark'
    >>> _place({"country": "United States"}), _place({})
    ('United States', '')
    """
    if not isinstance(loc, dict):
        return str(loc or "").strip()
    parts = [str(loc.get(k) or "").strip()
             for k in ("city", "region", "country")]
    return ", ".join(p for p in parts if p)


def location_str(job):
    """Every location a posting names, in one string.

    The flat ``city``/``state``/``country`` fields are the posting's primary
    site; ``locations[]`` is the full list, and a multi-site posting often
    shows only the first up front while the site that matters sits further
    down (see `api.merge_locations`, whose ";" join is also the separator
    `match.locality.is_nc` reads one office at a time).

    >>> location_str({"city": "Raleigh", "state": "North Carolina",
    ...               "country": "United States"})
    'Raleigh, North Carolina, United States'

    A ``hidden`` entry is one the employer chose not to show on the board,
    and a remote posting names no place at all:

    >>> location_str({"telecommuting": True,
    ...               "locations": [{"city": "Austin", "region": "Texas",
    ...                              "hidden": True}]})
    'Remote'
    >>> location_str({})
    'Unknown'
    """
    primary = _place({"city": job.get("city"), "region": job.get("state"),
                      "country": job.get("country")})
    extras = [_place(loc) for loc in (job.get("locations") or [])
              if not (isinstance(loc, dict) and loc.get("hidden"))]
    loc = merge_locations(primary, extras)
    if loc:
        return loc
    return "Remote" if job.get("telecommuting") else "Unknown"


def fetch_description(slug, shortcode, timeout=None):
    """Full JD text for one posting, "" on any failure.

    Workable splits the body across ``description`` (the prose) and
    ``requirements`` (the qualifications the fit model actually reads), so
    both are kept, in that order. ``benefits`` is left out: it is shared
    boilerplate on every posting of a board and would crowd the JD budget
    (`config.MAX_DESC_CHARS`).
    """
    try:
        r = SESSION.get(_JOB_API.format(slug=slug, shortcode=shortcode),
                        timeout=timeout, headers=JSON_HEADERS)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    parts = [data.get("description"), data.get("requirements")]
    html = "\n".join(p for p in parts if isinstance(p, str) and p)
    return text_from_html(html)


def _row(slug, j):
    """One listing entry as a `fetchers/board.py` row, or None."""
    shortcode = str(j.get("shortcode") or "").strip()
    title = str(j.get("title") or "").strip()
    if not shortcode or not title:
        return None
    dept = str(j.get("department") or "").strip()
    row = {"id": f"workable_{slug}_{shortcode}", "title": title,
           "url": job_url(slug, shortcode),
           "location": location_str(j), "description": "",
           "posted_at": norm_posted_date(j.get("published_on")
                                         or j.get("created_at")),
           "head": f"{title} {dept}".strip(),
           "_shortcode": shortcode}
    # telecommuting is the board's own structured flag; it beats regexing
    # the location string, which for a remote posting is often empty.
    if j.get("telecommuting") is True:
        row["remote_hint"] = "workable:telecommuting"
    return row


def fetch_workable(slug, company_name="", gate=None, loc_re=None,
                   max_details=40, detail_delay=0.2):
    """One Workable board through the board driver (fetchers/board.py):
    `gate` screens the title plus the posting's department and then, within
    `max_details` detail GETs, descriptions. One listing request, then at
    most one request per surviving row."""
    return board_fetch(
        f"Workable {company_name or slug}",
        lambda: parse_board(slug), lambda j: _row(slug, j),
        company_name, gate=gate, loc_re=loc_re,
        fetch_description=lambda row: fetch_description(slug, row["_shortcode"]),
        max_details=max_details, detail_delay=detail_delay)
