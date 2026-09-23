"""Is one stored posting still open? The per-job closure/liveness probe.

`probe_job_open` answers (is_open, reason) for one job's own detail URL and
`probe_family` names the ATS family that URL belongs to -- what
`ops.check_closed_jobs` dispatches on and reports its outcomes under.

This is a reader, not a fetcher: it shares nothing with the whole-board
pull in `fetchers/company.py` (where it lived until 2026-09-22) beyond the
per-ATS endpoint builders it asks -- `jazzhr.board_url` and Workday's CXS
URL helpers. A platform with a `config.BOARDS` spec is asked through the
engine (`Board.probe_job`).

One rule runs through every branch: a row is closed ONLY on positive
evidence. Every refusal a host can make -- 403, 405, 429, 5xx, a timeout,
a bot gate -- is "unverifiable", because a caller acts on a False by
closing the posting.
"""

import logging
import re
import time

import requests

from src import config
from src.net.http import HEADERS, JSON_HEADERS, SESSION, HostBreaker
from src.net.util import clean_url
from . import icims, jazzhr, workday
from .board import board_for_url

_log = logging.getLogger(__name__)

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
    "jazzhr":     re.compile(r"//([a-z0-9-]+)\.applytojob\.com/apply/([A-Za-z0-9]+)",
                             re.I),
    "icims":      re.compile(r"//([a-z0-9-]+)\.icims\.com/jobs/(\d+)/", re.I),
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
    board = board_for_url(url)
    if board:
        return board.name
    for fam, rex in _JOB_URL_RE.items():
        if rex.search(url):
            return fam
    return ""


def _endpoint_verdict(api, family, headers=None):
    """(is_open, reason) from an endpoint whose 404/410 PROVES the posting
    is gone. Every other refusal -- 403, 405, 429, 5xx, a timeout -- is
    unverifiable: a host declining to answer is not a closed posting.
    """
    try:
        r = SESSION.get(api, headers=headers or JSON_HEADERS)
    except Exception as e:
        return None, f"{family} api error: {type(e).__name__}"
    if r.status_code in (404, 410):
        return False, f"{family} api HTTP {r.status_code}"
    if r.status_code != 200:
        return None, f"{family} api HTTP {r.status_code}"
    return True, f"{family} api: posting live"


def _probe_jazzhr(m):
    # The slug-free apply URL: 410 once the posting is pulled, 200 while live.
    return _endpoint_verdict(
        f"{jazzhr.board_url(m.group(1))}/apply/{m.group(2)}", "jazzhr",
        headers=HEADERS)


# A job-detail host that refuses connections refuses every row on it: the
# 2026-09-22 13:21 pass spent 20 instant ConnectionErrors on one host. After
# three in a row, within the board memo's window, its remaining rows are
# skipped unasked. Three, not discovery's one: these hosts answered before.
_DEAD_HOSTS = HostBreaker(ttl=config.BOARD_MEMO_S, trips=3)


#: Per-family liveness checks, keyed as _JOB_URL_RE is; each takes that
#: family's match over the job URL. iCIMS is absent on purpose -- its own
#: detail page answers 410 for a pulled posting, it just needs the WAF's
#: headers (_page_headers).
_FAMILY_PROBE = {
    "jazzhr":          _probe_jazzhr,
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


def probe_job_open(url, job_id=None):
    """Best-effort liveness check of one job's own detail URL.

    Returns (is_open, reason): True = positively live, False = positively
    closed, None = indeterminate (bot-gated host, fetch error, or a 200 with
    no recognizable signal); callers must leave stored status alone on None.
    `job_id` names the posting where its URL cannot (a board whose postings
    all share one page).
    Only used for rows the crawl's board-diff can't cover (see
    tracks.local_tech.check_closed_jobs); board snapshots are authoritative
    where available.

    The posting's own page is the LAST resort, not the first: Lever,
    Greenhouse, Ashby, BambooHR and SmartRecruiters all serve a pulled
    posting as a plain HTTP 200, so the URL goes to that platform's
    public endpoint first (`Board.probe_job`, else `_FAMILY_PROBE`) and
    only an indeterminate answer there falls through to the page.

    A row is closed ONLY on positive evidence: 404/410 from one of those
    endpoints or from the page, an ATS "no longer available" notice, a
    past JSON-LD validThrough, a spec's `closure.closed` rule, a Workday
    CXS miss, or an id absent from a non-empty board listing.
    403/405/429/5xx/timeouts never close.
    """
    if not url:
        return None, "no url"
    # Heals rows stored before store.upsert_job applied the same rule.
    clean = clean_url(url)
    if clean != url:
        _log.debug("probe url had embedded whitespace: %s", clean)
        url = clean
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
    board = board_for_url(url)
    if board:
        is_open, fallback = board.probe_job(url, job_id)
        if is_open is not None:
            return is_open, fallback
    fam = probe_family(url)
    if fam in _FAMILY_PROBE:
        is_open, fallback = _FAMILY_PROBE[fam](_JOB_URL_RE[fam].search(url))
        if is_open is not None:
            return is_open, fallback

    if _DEAD_HOSTS.dead(url):
        return None, "host unreachable this pass: skipped"
    try:
        r = SESSION.get(url, headers=_page_headers(url), allow_redirects=True)
    except Exception as e:
        if isinstance(e, requests.ConnectionError):
            _DEAD_HOSTS.trip(url)
        return None, fallback or f"fetch error: {type(e).__name__}"
    if r.status_code in (404, 410):
        return False, f"HTTP {r.status_code}"
    if r.status_code != 200:
        return None, fallback or f"HTTP {r.status_code}"
    html = r.text[:200_000]
    m = _CLOSED_TEXT_RE.search(html)
    if m:
        return False, f"page says {m.group(0)[:50]!r}"
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
