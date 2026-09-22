"""Infor CloudSuite HCM "Candidate Experience" (CSS) external job boards.

Large employers running Infor's HCM suite publish their openings from a
tenant host of the form ``css-<tenant>.inforcloudsuite.com`` under the
``/hcm/Jobs`` app. The public careers site they advertise is usually a
different product entirely (Talemetry/Jobvite, Phenom, ...) sitting behind
a bot challenge; the Infor host underneath answers plain JSON to an
ordinary client with no auth, no cookie bootstrap and no robots.txt.

Two endpoints, both GET:

    /hcm/Jobs/list/JobPosting.SearchForJobsResults
        ?pageop=load&pagesize=N&pagepanel=JobsHomePage.Jobs.Jobs
        &csk.JobBoard=EXTERNAL&csk.HROrganization=<org>
    -> {"dataViewSet": {"data": [{"resourceId", "fields": {...}}],
                        "pagingInfo": {"hasNext", "fk", "lk", ...},
                        "pagingUrls": {"nextPageUrl", ...}}}

    /hcm/Jobs/form/<record key>.JobPostingDisplay
        ?pageop=load&pagesize=1&dependentForm=true&csk...
    -> {"fields": {"_op_PositionDescription_spc_translation_cp_": {...}}}

The store slug is ``"<host>|<org>"``: the tenant host alone does not name a
board, because one host can serve several HR organizations and the org id
is what scopes the listing.

Three things about the platform are worth knowing before reading the code:

* PAGING IS CURSOR-BASED AND OPAQUE. Each response's
  ``dataViewSet.pagingUrls.nextPageUrl`` is a fully-formed URL carrying the
  page's own ``fk``/``lk`` record keys; reconstructing it by hand (a
  ``pageop=next`` with the ``lk`` copied into an invented parameter) silently
  re-serves page 1, so this follows the server's URL verbatim and stops on
  ``pagingInfo.hasNext``.
* EVERY VALUE IS WRAPPED. A row's ``fields`` maps a field name to a small
  object (``{"value": ..., "size": ..., "stateValues": [...]}``), never to a
  bare value, hence `_val`.
* A POSTING'S KEY IS A TRIPLE, not an id: (org, requisition, posting
  revision). It travels URL-ENCODED IN THE PATH -- ``JobPosting[JobPostingSet]
  (9999,207651,1).JobPostingDisplay`` -- and the same form URL without the
  data parameters is the page a person can open, which is what `job_url`
  builds. The requisition id here is the Infor one; the ids on the tenant's
  public careers site belong to whatever product republishes these postings
  and do not match, so no URL is ever minted against that host.
"""

import re
import time
from urllib.parse import quote

from src.net.http import JSON_HEADERS, fetch_failed, get_json, note_capped
from src.net.util import text_from_html  # shared HTML->text stripper
from .board import board_jobs, loc_ok

#: The candidate-facing board. Infor names the internal one INTERNAL; only
#: the external board is public, so it is a constant rather than a coordinate.
_JOB_BOARD = "EXTERNAL"
_LIST_PANEL = "JobsHomePage.Jobs.Jobs"
_PAGE_SIZE = 500                 # honored as asked; 1,500-row boards are 3 pages
_MAX_PAGES = 40                  # safety valve: 20,000 rows at _PAGE_SIZE

_DESC_FIELD = "_op_PositionDescription_spc_translation_cp_"
#: "US:NC:Morrisville | Professional - Non-Clinical | Full Time" -- the
#: detail form's one-line subtitle, and the only location it carries.
_SUBTITLE_FIELD = "_op_JobRequisitionLocationCategoryWorkType_spc_translation_cp_"

_CODE_RE = re.compile(r"^[A-Za-z]{2}$")
_YMD_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
#: This module's own job URLs only: the record triple is URL-encoded into
#: the path segment ahead of ".JobPostingDisplay". Public because
#: `fetchers/probe.py`'s closure probe keys its family table on the same
#: regex it passes to the family's check -- one definition, no drift.
JOB_URL_RE = re.compile(
    r"^https?://([^/]+)/hcm/Jobs/form/JobPosting%5BJobPostingSet%5D"
    r"%28(\d+)%2C(\d+)%2C(\d+)%29\.JobPostingDisplay", re.I)


def board_coords(slug):
    """(host, org) from a store slug. Missing halves come back "".

    >>> board_coords("css-acme-prd.inforcloudsuite.com|9999")
    ('css-acme-prd.inforcloudsuite.com', '9999')
    >>> board_coords("https://css-acme-prd.inforcloudsuite.com/hcm/Jobs|42")
    ('css-acme-prd.inforcloudsuite.com', '42')
    >>> board_coords("")
    ('', '')
    """
    host, _, org = (slug or "").partition("|")
    host = re.sub(r"^https?://", "", host.strip()).split("/")[0]
    return host, org.strip()


def _record_path(host, org, req, posting):
    key = quote(f"JobPosting[JobPostingSet]({org},{req},{posting})", safe="")
    return f"https://{host}/hcm/Jobs/form/{key}.JobPostingDisplay"


def job_url(host, org, req, posting):
    """The page a person can open for one posting -- the server's own
    `formUrl`, which renders the posting and its Apply button.

    >>> job_url("css-acme-prd.inforcloudsuite.com", "9999", 207651, 1)
    'https://css-acme-prd.inforcloudsuite.com/hcm/Jobs/form/JobPosting%5BJobPostingSet%5D%289999%2C207651%2C1%29.JobPostingDisplay?pagesize=1&csk.JobBoard=EXTERNAL&csk.HROrganization=9999'
    """
    return (f"{_record_path(host, org, req, posting)}"
            f"?pagesize=1&csk.JobBoard={_JOB_BOARD}&csk.HROrganization={org}")


def detail_url(host, org, req, posting):
    """The JSON behind that page: the same record, with the form's own data
    parameters. What hydration reads and what the closure probe asks.

    >>> detail_url("css-acme-prd.inforcloudsuite.com", "9999", 207651, 1)
    'https://css-acme-prd.inforcloudsuite.com/hcm/Jobs/form/JobPosting%5BJobPostingSet%5D%289999%2C207651%2C1%29.JobPostingDisplay?pageop=load&pagesize=1&dependentForm=true&csk.JobBoard=EXTERNAL&csk.HROrganization=9999'
    """
    return (f"{_record_path(host, org, req, posting)}"
            f"?pageop=load&pagesize=1&dependentForm=true"
            f"&csk.JobBoard={_JOB_BOARD}&csk.HROrganization={org}")


def job_ref_from_url(url):
    """(host, org, req, posting) from one of this module's own job URLs, or
    None. Lets a caller holding nothing but a stored job's `url` re-derive
    the detail coordinates, the way the other ATS hydrate branches do --
    `fetchers/company.py`'s `_adapt` keeps no ATS-specific key.

    >>> job_ref_from_url(job_url("css-acme-prd.inforcloudsuite.com", "9999", 207651, 1))
    ('css-acme-prd.inforcloudsuite.com', '9999', '207651', '1')
    >>> job_ref_from_url("https://example.org/jobs/1") is None
    True
    """
    m = JOB_URL_RE.match(url or "")
    return (m.group(1), m.group(2), m.group(3), m.group(4)) if m else None


def _val(fields, name, default=""):
    """One field's value out of the `{name: {"value": ...}}` wrapper."""
    f = (fields or {}).get(name)
    v = f.get("value") if isinstance(f, dict) else f
    return default if v is None else v


def location_str(raw):
    """A colon-delimited Infor location read back as a readable address.

    The board composes up to four location levels broadest-first, so the
    usual shape is country:state:city:

    >>> location_str("US:NC:Morrisville")
    'Morrisville, NC, US'

    Tenants fill the levels inconsistently, and every partial form has to
    stay readable -- and, for the geo gate, keep its "<city>, ST" shape:

    >>> location_str("Chapel Hill:NC"), location_str("US:NC")
    ('Chapel Hill, NC', 'NC, US')
    >>> location_str("Smithfield"), location_str("")
    ('Smithfield', '')
    """
    parts = [p.strip() for p in str(raw or "").split(":") if p.strip()]
    country = parts.pop(0) if len(parts) > 1 and _CODE_RE.match(parts[0]) else ""
    state = ""
    for i, p in enumerate(parts):
        if _CODE_RE.match(p):
            state = parts.pop(i)
            break
    return ", ".join(x for x in (", ".join(parts), state, country) if x)


def _posted(value):
    """A `PostingDateRange_prd_Begin` (YYYYMMDD) as an ISO date, or None.

    Not `net.util.norm_posted_date`: 20260921 read as a number is neither
    an epoch nor a year, and it returns None for it.

    >>> _posted("20260921"), _posted("00000000"), _posted("")
    ('2026-09-21', None, None)
    """
    m = _YMD_RE.match(str(value or "").strip())
    if not m or m.group(1) == "0000":
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _category(fields):
    """The posting's category as a person reads it ("Professional -
    Non-Clinical"), falling back to the stored code ("PROF - CLINICAL")."""
    return (_val(fields, "_op_Category_prd_Description_spc_translation_cp_")
            or _val(fields, "Category"))


def _row(host, org, fields):
    """One listing entry as a `fetchers/board.py` row, or None."""
    title = str(_val(fields, "Description")).strip()
    req = str(_val(fields, "JobRequisition") or _val(fields, "JobId")).strip()
    posting = str(_val(fields, "JobPosting")).strip()
    if not title or not req or not posting:
        return None
    tenant = host.split(".")[0]
    category = _category(fields)
    return {"id": f"infor_{tenant}_{req}_{posting}", "title": title,
            "url": job_url(host, org, req, posting),
            "location": location_str(_val(fields, "LocationOfJobDescriptionForSort")
                                     or _val(fields, "LocationOfJob")),
            "description": "", "head": f"{title} {category}".strip(),
            "posted_at": _posted(_val(fields, "PostingDateRange_prd_Begin")),
            "_infor": (host, org, req, posting)}


def _next_url(host, data_view):
    """This page's cursor URL, or None. The board sends `pagingUrls`
    whether or not there is a next page, so `hasNext` is what ends a walk
    (read by the caller); a URL that is not this board's own list endpoint
    is refused rather than followed -- it is served data, not a promise."""
    nxt = (data_view.get("pagingUrls") or {}).get("nextPageUrl") or ""
    prefix = f"https://{host}/hcm/Jobs/list/"
    return nxt if nxt.lower().startswith(prefix.lower()) else None


def fetch_infor_all(slug, loc_re=None, page_size=_PAGE_SIZE, max_pages=_MAX_PAGES):
    """List every posting on one Infor board (title/location/key only;
    descriptions hydrate lazily -- see `fetch_infor_description`).

    Pages by following the server's own `nextPageUrl` (see module doc) and
    dedupes by record key, so a cursor that repeats a page ends the walk
    rather than looping. Reading all `max_pages` with the server still
    claiming a next page reports a capped snapshot (net.http.note_capped):
    the rows returned are real, but a posting missing from them is no
    evidence it closed.
    """
    host, org = board_coords(slug)
    if not host or not org:
        return fetch_failed(f"infor {slug}", "board slug is not <host>|<org>")
    url = f"https://{host}/hcm/Jobs/list/JobPosting.SearchForJobsResults"
    params = {"pageop": "load", "pagesize": page_size, "pagepanel": _LIST_PANEL,
              "csk.JobBoard": _JOB_BOARD, "csk.HROrganization": org}
    seen, out, capped = set(), [], False
    for page in range(max_pages):
        payload = get_json(url, f"infor {host} page {page + 1}",
                           headers=JSON_HEADERS, params=params if page == 0 else None)
        if not payload:
            return out
        dv = payload.get("dataViewSet") or {}
        new = 0
        for entry in dv.get("data") or []:
            row = _row(host, org, entry.get("fields"))
            # The record key, or the row's own id when an entry carries
            # none: keying every keyless entry on None would drop all but
            # the first of them.
            key = entry.get("resourceId") or (row and row["id"])
            if key in seen:
                continue
            seen.add(key)
            new += 1
            if row and loc_ok(loc_re, row["location"]):
                out.append(row)
        if not (dv.get("pagingInfo") or {}).get("hasNext"):
            break                   # the board's own end: a complete snapshot
        url = _next_url(host, dv)
        if not url or not new:
            capped = True           # a cursor refused, or one that looped back
            break
        params = None
    else:
        capped = True
    if capped:
        note_capped()
    return out


def _detail_fields(host, org, req, posting):
    """The `fields` object of one posting's detail form, or {}."""
    payload = get_json(detail_url(host, org, req, posting),
                       f"infor {host} job {req}", headers=JSON_HEADERS)
    return (payload or {}).get("fields") or {}


def _detail_description(fields):
    return text_from_html(_val(fields, _DESC_FIELD))


def _detail_location(fields):
    """The location out of the detail form's "<place> | <category> | <work
    type>" subtitle, normalized like a listing row's."""
    return location_str(str(_val(fields, _SUBTITLE_FIELD)).split("|")[0].strip())


def fetch_infor_description(url):
    """(description_text, location) for one stored job URL, both "" on
    failure. The hydrate-from-URL entry point (see `job_ref_from_url`).

    >>> fetch_infor_description("https://example.org/")   # not a job URL
    ('', '')

    Notes:
        NEAR-MISS, DELIBERATE (2026-09-22 clone scan): structurally
        identical to `phenom.fetch_phenom_description`, and stays so. The
        shape IS the contract `company.hydrate_description` calls an ATS
        by -- URL in, (description, location) out -- and all four
        operations inside are this platform's own, so a shared helper
        would take four callables to save three lines.
    """
    ref = job_ref_from_url(url)
    if not ref:
        return "", ""
    fields = _detail_fields(*ref)
    return _detail_description(fields), _detail_location(fields)


def posting_state(payload, today=None):
    """(is_open, reason) for one detail-form payload -- the evidence behind
    `fetchers/probe.py`'s closure probe. None means "nothing was proved".

    The endpoint answers HTTP 200 whatever the posting's fate, so the
    status code is never the witness. Verified live on 2026-09-21 against
    a board of 1,575 open postings and a window of requisitions no longer
    on it: a pulled posting either loses its record outright...

    >>> posting_state({"status": "DOES_NOT_EXIST", "statusCode": 404})
    (False, 'infor api: posting record gone')

    ...or keeps it with a posting-end date now in the past (every sampled
    unlisted record that still existed had one, four years stale):

    >>> ended = {"fields": {"PostingDateRange_prd_End": {"value": "20220630"}}}
    >>> posting_state(ended, today="2026-09-21")
    (False, 'infor api: posting ended 2022-06-30')

    A listed posting's end date is either open-ended ("00000000") or in the
    future, so neither closes -- and the same record read before its end
    date is live, which is what keeps a dated posting from closing early:

    >>> posting_state({"fields": {"PostingDateRange_prd_End": {"value": "00000000"}}})
    (True, 'infor api: posting live')
    >>> posting_state(ended, today="2020-01-01")
    (True, 'infor api: posting live')

    Anything else -- an error body, a payload with no record in it -- is
    unverifiable, never closed:

    >>> posting_state({})
    (None, 'infor api: no posting record')
    """
    payload = payload or {}
    if (str(payload.get("status") or "").upper() == "DOES_NOT_EXIST"
            or payload.get("statusCode") == 404):
        return False, "infor api: posting record gone"
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        return None, "infor api: no posting record"
    ended = _posted(_val(fields, "PostingDateRange_prd_End"))
    if ended and ended < (today or time.strftime("%Y-%m-%d")):
        return False, f"infor api: posting ended {ended}"
    return True, "infor api: posting live"


def fetch_infor(slug, company_name="", gate=None, loc_re=None, max_details=40,
                detail_delay=0.2):
    """`fetch_infor_all`'s rows through the board driver (fetchers/board.py):
    `gate` screens the title plus the posting's category and then, within
    `max_details` detail GETs, descriptions."""
    rows = fetch_infor_all(slug, loc_re=loc_re)
    return board_jobs(
        rows, company_name, gate=gate,
        fetch_description=lambda row: _detail_description(
            _detail_fields(*row["_infor"])),
        max_details=max_details, detail_delay=detail_delay)
