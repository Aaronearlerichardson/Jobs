"""Is one stored posting still open? The per-job closure/liveness probe.

`probe_job_open` answers (is_open, reason) for one job's own detail URL and
`probe_family` names the ATS family that URL belongs to -- what
`ops.check_closed_jobs` dispatches on and reports its outcomes under.

This is a reader, not a fetcher: it shares nothing with the whole-board
pull in `fetchers/company.py` (where it lived until 2026-09-22) beyond the
per-ATS endpoint builders it asks -- `bamboohr.detail_url`,
`jazzhr.board_url`, `infor.detail_url`/`posting_state`, Workday's CXS URL
helpers and `api.py`'s board APIs.

One rule runs through every branch: a row is closed ONLY on positive
evidence. Every refusal a host can make -- 403, 405, 429, 5xx, a timeout,
a bot gate -- is "unverifiable", because a caller acts on a False by
closing the posting.
"""

import re
import time

from src.net.http import HEADERS, JSON_HEADERS, SESSION
from . import bamboohr, icims, infor, jazzhr, workday
from .api import ASHBY_API, GREENHOUSE_API, GREENHOUSE_JOB_URL_RE, LEVER_API

# Standard "this posting is gone" notices across ATS templates. Curated and
# phrase-anchored (never a bare "closed"/"expired") so an open JD that merely
# mentions e.g. "closed-loop systems" can't trip it.
_CLOSED_TEXT_RE = re.compile("|".join((
    r"no longer (open|available|active|posted|accepting applications)",
    r"(position|role|job|posting|vacancy|requisition) (has been|is|was) "
    r"(filled|closed|cancell?ed|removed)",
    r"(job|position|posting|vacancy) (has |is )?expired",
    r"not currently accepting applications",
    r"this (job|position|posting) is (closed|inactive|unavailable)",
    r"job (posting )?not found",
)), re.I)

# Hosts that bot-gate anonymous GETs (authwalls/999s): a probe there says
# nothing about the posting, so report "unverifiable", never "closed".
_GATED_HOST_RE = re.compile(
    r"linkedin\.com|indeed\.com|glassdoor\.|ziprecruiter\.com|"
    r"simplyhired\.com|monster\.com", re.I)

# Which ATS owns a stored job URL, and the (board handle, posting id) its
# public API needs. Most platforms serve a PULLED posting's own page as an
# ordinary HTTP 200 with no closure marker -- which is why 36 of the 37
# probes on 2026-09-21 came back "unverifiable" -- so the question is put
# to the endpoint the fetcher already reads (_FAMILY_PROBE) rather than to
# the page.
_JOB_URL_RE = {
    "lever":      re.compile(r"lever\.co/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{20,})"),
    # api.py's own regex, which ops.maintenance._live_jd also reads a stored
    # job URL through -- the two copies of it had already drifted.
    "greenhouse": GREENHOUSE_JOB_URL_RE,
    "ashby":      re.compile(r"ashbyhq\.com/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{20,})"),
    "smartrecruiters": re.compile(r"smartrecruiters\.com/([A-Za-z0-9_.-]+)/(\d+)"),
    "bamboohr":   re.compile(r"//([a-z0-9-]+)\.bamboohr\.com/careers/(\d+)", re.I),
    "jazzhr":     re.compile(r"//([a-z0-9-]+)\.applytojob\.com/apply/([A-Za-z0-9]+)",
                             re.I),
    "icims":      re.compile(r"//([a-z0-9-]+)\.icims\.com/jobs/(\d+)/", re.I),
    # (host, org, requisition, posting revision) — the fetcher's own regex,
    # so the two never drift apart.
    "infor":      infor.JOB_URL_RE,
}


def probe_family(url):
    """The ATS family a stored job URL belongs to: what probe_job_open
    dispatches on, and the bucket ops.check_closed_jobs reports its
    outcomes under. "" when nothing recognizes the URL (self-hosted
    boards, one-off captures).

    >>> probe_family("https://jobs.lever.co/acme/2e1a8d40-0f2b-4c7e-9a11-5b6c7d8e9f01")
    'lever'
    >>> probe_family("https://acme.wd1.myworkdayjobs.com/External/job/RTP/Eng_R1")
    'workday'
    >>> probe_family("https://careers-acme.icims.com/jobs/42/eng/job?in_iframe=1")
    'icims'
    >>> probe_family("https://css-acme-prd.inforcloudsuite.com/hcm/Jobs/form/"
    ...              "JobPosting%5BJobPostingSet%5D%2842%2C207651%2C1%29"
    ...              ".JobPostingDisplay?pagesize=1")
    'infor'
    >>> probe_family("https://www.linkedin.com/jobs/view/4435444961/")
    'gated'
    >>> probe_family("https://acme.com/careers/engineer")
    ''
    """
    if not url:
        return ""
    if _GATED_HOST_RE.search(url):
        return "gated"
    if workday._cxs_detail_url(url):
        return "workday"
    for fam, rex in _JOB_URL_RE.items():
        if rex.search(url):
            return fam
    return ""


def _endpoint_verdict(api, family, headers=None, live=None):
    """(is_open, reason) from an endpoint whose 404/410 PROVES the posting
    is gone. Every other refusal -- 403, 405, 429, 5xx, a timeout -- is
    unverifiable: a host declining to answer is not a closed posting.
    `live(response)` replaces the default "200 means live" for a platform
    that keeps serving pulled postings (SmartRecruiters) or that can only
    be asked about its whole board (Ashby).
    """
    try:
        r = SESSION.get(api, headers=headers or JSON_HEADERS)
    except Exception as e:
        return None, f"{family} api error: {type(e).__name__}"
    if r.status_code in (404, 410):
        return False, f"{family} api HTTP {r.status_code}"
    if r.status_code != 200:
        return None, f"{family} api HTTP {r.status_code}"
    return live(r) if live else (True, f"{family} api: posting live")


def _probe_lever(m):
    return _endpoint_verdict(f"{LEVER_API}/{m.group(1)}/{m.group(2)}", "lever")


def _probe_greenhouse(m):
    return _endpoint_verdict(
        f"{GREENHOUSE_API}/{m.group(1)}/jobs/{m.group(2)}", "greenhouse")


def _probe_bamboohr(m):
    return _endpoint_verdict(bamboohr.detail_url(m.group(1), m.group(2)),
                             "bamboohr")


def _probe_jazzhr(m):
    # The slug-free apply URL: 410 once the posting is pulled, 200 while live.
    return _endpoint_verdict(
        f"{jazzhr.board_url(m.group(1))}/apply/{m.group(2)}", "jazzhr",
        headers=HEADERS)


def _smartrecruiters_verdict(r):
    """SmartRecruiters keeps serving a pulled posting at HTTP 200, so the
    status code says nothing; `active` is the field that does."""
    try:
        d = r.json()
    except ValueError:
        return None, "smartrecruiters api: non-JSON"
    if d.get("active") is False:
        return False, "smartrecruiters api: active=false"
    if d.get("active") is not True:
        return None, "smartrecruiters api: no active flag"
    jid = str(d.get("id") or "")
    # A repost answers under its SUCCESSOR's id (postingUrl carries that
    # one), which is a verdict on the successor, not on this row.
    if jid and jid not in (d.get("postingUrl") or jid):
        return None, "smartrecruiters api: reposted under a new id"
    return True, "smartrecruiters api: active"


_ASHBY_BOARD_TTL = 600.0
_ASHBY_BOARDS = {}      # handle -> (expires_at, {posting ids} or None)


def _ashby_board_ids(handle):
    """The posting ids on one Ashby board, memoized for the pass so a
    company with many stale rows fetches its board once, not once per row.
    None for BOTH an unreadable and an empty listing: neither is evidence
    a posting closed (fetchers soft-fail to [] -- see
    store.sync_job_statuses' caller contract)."""
    hit = _ASHBY_BOARDS.get(handle)
    if hit and hit[0] > time.time():
        return hit[1]
    ids = None
    try:
        r = SESSION.get(f"{ASHBY_API}/{handle}", headers=JSON_HEADERS)
        if r.status_code == 200:
            ids = {str(j.get("id", "")).lower()
                   for j in (r.json().get("jobs") or [])} or None
    except Exception:
        ids = None
    _ASHBY_BOARDS[handle] = (time.time() + _ASHBY_BOARD_TTL, ids)
    return ids


def _probe_ashby(m):
    """Ashby publishes no per-posting endpoint, so the board listing the
    fetcher already reads is the witness: an id missing from a NON-EMPTY
    board is positive evidence that the posting is gone."""
    ids = _ashby_board_ids(m.group(1))
    if not ids:
        return None, "ashby api: board unreadable or empty"
    if m.group(2).lower() in ids:
        return True, "ashby api: board lists it"
    return False, "ashby api: board no longer lists it"


def _infor_verdict(r):
    """Infor answers HTTP 200 for a pulled posting as readily as for a live
    one, so the verdict is in the body (fetchers/infor.posting_state)."""
    try:
        return infor.posting_state(r.json())
    except ValueError:
        return None, "infor api: non-JSON"


#: Per-family liveness checks, keyed as _JOB_URL_RE is; each takes that
#: family's match over the job URL. iCIMS is absent on purpose -- its own
#: detail page answers 410 for a pulled posting, it just needs the WAF's
#: headers (_page_headers).
_FAMILY_PROBE = {
    "lever":           _probe_lever,
    "greenhouse":      _probe_greenhouse,
    "ashby":           _probe_ashby,
    "bamboohr":        _probe_bamboohr,
    "jazzhr":          _probe_jazzhr,
    # SmartRecruiters has no fetcher module of its own -- this module is
    # where its endpoints live (fetch_smartrecruiters_all above).
    "smartrecruiters": lambda m: _endpoint_verdict(
        f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}"
        f"/postings/{m.group(2)}", "smartrecruiters",
        live=_smartrecruiters_verdict),
    "infor": lambda m: _endpoint_verdict(
        infor.detail_url(*m.groups()), "infor", live=_infor_verdict),
}


def _page_headers(url):
    """Headers for a detail-page GET: iCIMS's own for an iCIMS posting
    (fetchers/icims.ICIMS_HEADERS -- its WAF answers the crawler's default
    UA with HTTP 405), the shared defaults for everything else. Pinned by
    tests/test_probes.py::TestProbeIsDecisivePerFamily.

    Notes:
        17 of the 36 unverifiable probes on 2026-09-21 were that 405, not
        a dead posting.
    """
    return (icims.ICIMS_HEADERS if _JOB_URL_RE["icims"].search(url or "")
            else HEADERS)


def probe_job_open(url):
    """Best-effort liveness check of one job's own detail URL.

    Returns (is_open, reason): True = positively live, False = positively
    closed, None = indeterminate (bot-gated host, fetch error, or a 200 with
    no recognizable signal); callers must leave stored status alone on None.
    Only used for rows the crawl's board-diff can't cover (see
    tracks.local_tech.check_closed_jobs); board snapshots are authoritative
    where available.

    The posting's own page is the LAST resort, not the first: Lever,
    Greenhouse, Ashby, BambooHR and SmartRecruiters all serve a pulled
    posting as a plain HTTP 200, so `probe_family` routes the URL to that
    platform's public endpoint first and only an indeterminate answer
    there falls through to the page (which still catches e.g.
    greenhouse's redirect off a pulled job page).

    A row is closed ONLY on positive evidence: 404/410 from one of those
    endpoints or from the page, an ATS "no longer available" notice, a
    past JSON-LD validThrough or Infor posting-end date, a Workday CXS
    miss, or an id absent from a non-empty board listing.
    403/405/429/5xx/timeouts never close.
    """
    if not url:
        return None, "no url"
    if _GATED_HOST_RE.search(url):
        return None, "bot-gated aggregator host"

    # Workday: the CXS JSON detail endpoint is authoritative and JS-free.
    # Hyphenated tenants need the underscore tenant id in the CXS path,
    # so each variant is tried before concluding anything.
    cxs = workday._cxs_detail_url(url)
    if cxs:
        last_status = None
        for u in workday._cxs_tenant_variants(cxs):
            try:
                r = SESSION.get(u,
                                headers=JSON_HEADERS)
            except Exception as e:
                return None, f"workday cxs error: {type(e).__name__}"
            last_status = r.status_code
            if r.status_code != 200:
                continue
            try:
                info = r.json().get("jobPostingInfo") or {}
            except ValueError:
                return None, "workday cxs non-JSON"
            if info.get("jobDescription") or info.get("title"):
                return True, "workday cxs: posting live"
            return False, "workday cxs: no jobPostingInfo"
        if last_status in (404, 410):
            return False, f"workday cxs HTTP {last_status}"
        return None, f"workday cxs HTTP {last_status}"

    # The platform's own API first; its reason is the one worth reporting
    # if the page below cannot tell either.
    fallback = ""
    fam = probe_family(url)
    if fam in _FAMILY_PROBE:
        is_open, fallback = _FAMILY_PROBE[fam](_JOB_URL_RE[fam].search(url))
        if is_open is not None:
            return is_open, fallback

    try:
        r = SESSION.get(url, headers=_page_headers(url), allow_redirects=True)
    except Exception as e:
        return None, fallback or f"fetch error: {type(e).__name__}"
    if r.status_code in (404, 410):
        return False, f"HTTP {r.status_code}"
    if r.status_code != 200:
        return None, fallback or f"HTTP {r.status_code}"
    html = r.text[:200_000]
    m = _CLOSED_TEXT_RE.search(html)
    if m:
        return False, f"page says {m.group(0)[:50]!r}"
    # Greenhouse silently redirects a closed job's URL back to the board root.
    if "greenhouse.io" in url:
        tail = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        if tail and tail not in (r.url or ""):
            return False, "greenhouse redirect off job page"
    try:
        from .jsonld import extract_jsonld, is_jobposting
        for obj in extract_jsonld(html):
            if is_jobposting(obj):
                vt = str(obj.get("validThrough") or "")[:10]
                if vt and vt < time.strftime("%Y-%m-%d"):
                    return False, f"validThrough {vt} past"
                return True, "JSON-LD JobPosting live"
    except Exception:
        pass
    return None, fallback or "no closed signal"
