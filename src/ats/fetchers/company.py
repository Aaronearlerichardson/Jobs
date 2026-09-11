"""
Company-scoped fetching: ALL of a *mission-vetted* company's postings,
optionally location-filtered, in one shape whatever the ATS.

The company was already vetted (mission scored at discovery time, stored in
src/store/__init__.py), so the whole board is pulled with no relevance gate and the
caller's own filter chain decides. `fetch_company` dispatches a store row
to the ATS's fetcher module (fetchers/<ats>.py, the same functions the
unvetted-board sweep calls with `gate=is_relevant`) and `_adapt` puts the
result in the company-fetch shape:

    {"id", "title", "url", "location", "description", "ats", "_wd",
     ["posted_at"], ["remote_hint"]}

`_wd` is Workday's (tenant, pod, site, path) for `hydrate_description`,
None elsewhere. The module also keeps what has no fetcher module of its
own: SmartRecruiters and WordPress careers endpoints, self-hosted
("custom") careers pages and their board detection, the per-URL
description/title readers, and the open/closed probe.
"""

import hashlib
import json
import re
import time
from urllib.parse import unquote

from bs4 import BeautifulSoup, SoupStrainer

from src import config

# Parse pages with lxml (2-3x faster than html.parser, and the gap widens with
# page size). For paths that only need job/nav anchors (link counting and the
# openings-hop) restrict parsing to <a> tags via SoupStrainer; anchors and
# their descendants are preserved (find_job_links reads a title element inside
# each <a>), which is all those callers touch. Paths that read an anchor's
# surrounding container (location extraction in fetch_custom_careers) keep the
# full tree via _get_soup.
_ANCHORS_ONLY = SoupStrainer("a")

from src.net.http import HEADERS, SESSION, JSON_HEADERS
from src.match.locality import NC_RE  # profile [locality]: the location gate
from src.net.util import LOC_TEXT_RE, cache_dir, default_search_text, norm_posted_date
from . import icims, workday
from .adp_wfn import fetch_adp
from .api import fetch_ashby, fetch_greenhouse, fetch_lever
from .bamboohr import fetch_bamboohr
from .board import loc_ok
from .hibob import fetch_hibob
from .html_scrape import fetch_kula, fetch_successfactors
from .icims import fetch_icims_all
from .jazzhr import fetch_jazzhr
from .jobvite import fetch_jobvite
from .paylocity import fetch_paylocity
from .peopleadmin import fetch_peopleadmin
from .rippling import fetch_rippling
from .ultipro import fetch_ultipro
from .workday import fetch_workday_all, wd_local_count  # noqa: F401 (re-export)

# JD text budget (config.MAX_DESC_CHARS): one cap shared with storage and the
# scoring prompt, so a long posting's requirements block survives end to end.
_DESC_MAX = config.MAX_DESC_CHARS

# Kept for discovery (local_sourcing), which imports the Workday search
# term under this name.
_default_search_text = default_search_text


def _get_json(url, label, **kw):
    """GET + parse JSON, treating any HTTP error, empty body, or non-JSON
    response as a clean miss (returns None) rather than an exception that
    surfaces as a cryptic ``Expecting value`` further up the stack."""
    r = SESSION.get(url, headers=HEADERS, **kw)
    if r.status_code != 200:
        print(f"    [!] {label}: HTTP {r.status_code}")
        return None
    if not r.content.strip():
        print(f"    [!] {label}: empty response")
        return None
    try:
        return r.json()
    except ValueError:
        print(f"    [!] {label}: non-JSON response")
        return None


# --- the shape --------------------------------------------------------------- #

_JOB_KEYS = ("id", "title", "url", "location", "description", "posted_at",
             "remote_hint", "_wd")


def _adapt(jobs, ats, loc_re=None):
    """A fetcher module's job dicts in the company-fetch shape: `ats`
    named, `company` dropped (the store row supplies it), the description
    capped at the JD budget, `_wd` kept where the module set it.

    The modules apply `loc_re` themselves; pass one here only for rows a
    module could not filter (see fetch_peopleadmin_all).

    >>> _adapt([{"id": "x_1", "company": "Acme", "title": "T", "url": "u",
    ...          "location": "Durham, NC", "description": "d", "posted_at": "2026-01-02"}], "x")
    [{'id': 'x_1', 'title': 'T', 'url': 'u', 'location': 'Durham, NC', 'description': 'd', 'posted_at': '2026-01-02', 'ats': 'x', '_wd': None}]
    """
    out = []
    for j in jobs:
        if not loc_ok(loc_re, j.get("location", "")):
            continue
        job = {k: j[k] for k in _JOB_KEYS if k in j}
        job["description"] = (j.get("description") or "")[:_DESC_MAX]
        job["ats"] = ats
        job.setdefault("_wd", None)
        out.append(job)
    return out


def fetch_smartrecruiters_all(slug, loc_re=None, max_pages=10):
    """SmartRecruiters public postings API. Descriptions hydrated lazily."""
    out = []
    for page in range(max_pages):
        try:
            r = SESSION.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                             f"?limit=100&offset={page*100}", headers=HEADERS)
            data = r.json()
        except Exception as e:
            print(f"    [!] smartrecruiters {slug}: {e}")
            break
        content = data.get("content", []) or []
        if not content:
            break
        for p in content:
            loc = p.get("location", {}) or {}
            loc_s = ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                          loc.get("country")) if x)
            if not loc_ok(loc_re, loc_s):
                continue
            pid = p.get("id")
            out.append({"id": f"sr_{slug}_{pid}", "title": p.get("name", ""),
                        "url": f"https://jobs.smartrecruiters.com/{slug}/{pid}",
                        "location": loc_s, "description": "", "ats": "smartrecruiters",
                        "_wd": None, "_sr": (slug, pid),
                        "posted_at": norm_posted_date(p.get("releasedDate"))})
        if len(content) < 100:
            break
    return out


def fetch_peopleadmin_all(host, loc_re=None):
    """Full PeopleAdmin board, adapted to the company-fetch shape.

    `loc_re` is applied only to the postings whose text named a place. A
    tenant's Atom entries carry no location field (see fetchers.peopleadmin),
    so most postings have nothing for a location filter to match and gating
    them would drop the whole campus, which a university board never
    deserves, being local by construction.

    See tests/test_fetcher_parsers.py::TestPeopleAdmin.
    """
    out = []
    for j in fetch_peopleadmin(host, ""):
        out += _adapt([j], "peopleadmin", loc_re if j.get("location") else None)
    return out


def hydrate_description(job):
    """Fetch a job's real description (in place) for ATSes with a detail call."""
    if job.get("description"):
        return job
    if job.get("ats") == "workday" and job.get("_wd"):
        info = workday.cxs_detail(*job["_wd"])
        if info:
            job["description"] = workday.text_from_html(
                info.get("jobDescription", ""))[:_DESC_MAX]
            # Same JSON carries the req's full location list: upgrade a
            # useless "2 Locations" locationsText to the real thing.
            locs = workday.detail_locations(info)
            if locs and (not job.get("location")
                         or workday.N_LOCATIONS_RE.match(job["location"] or "")):
                job["location"] = "; ".join(locs)
    elif job.get("ats") == "smartrecruiters" and job.get("_sr"):
        slug, pid = job["_sr"]
        try:
            r = SESSION.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}", headers=HEADERS)
            secs = r.json().get("jobAd", {}).get("sections", {}) or {}
            parts = [secs.get(k, {}).get("text", "") for k in
                     ("jobDescription", "qualifications", "additionalInformation")]
            html = " ".join(p for p in parts if p)
            job["description"] = BeautifulSoup(html, "html.parser").get_text(" ")[:_DESC_MAX]
        except Exception:
            pass
    elif job.get("ats") == "paylocity":
        m = re.search(r"/Details/(\d+)", job.get("url", "") or "")
        if m:
            from .paylocity import fetch_description
            job["description"] = fetch_description(m.group(1))[:_DESC_MAX]
    elif job.get("ats") == "rippling":
        m = re.search(r"rippling\.com/([^/]+)/jobs/([0-9a-f-]{36})", job.get("url", "") or "")
        if m:
            from .rippling import fetch_description
            job["description"] = fetch_description(m.group(1), m.group(2))[:_DESC_MAX]
    elif job.get("ats") == "icims" and job.get("url"):
        # The ?in_iframe=1 document is server-rendered with JSON-LD even on
        # JS-shell tenants; it also names the posting's real location(s).
        loc, desc = icims.job_meta(job["url"], need_desc=True)
        if desc:
            job["description"] = desc[:_DESC_MAX]
        if loc and (job.get("location") or "").strip() in ("", icims.LOCAL_LABEL):
            job["location"] = loc
    elif job.get("ats") == "wpjson" and job.get("url"):
        # Outbound apply page (an Arcoro/BirdDog portal). Server-rendered;
        # the JD sits in #portalViewRequirement. The generic fallback below
        # still runs on a miss (e.g. a WP permalink URL).
        try:
            html = SESSION.get(job["url"], headers=HEADERS).text
            soup = BeautifulSoup(html, "lxml")
            el = (soup.select_one("#portalViewRequirement")
                  or soup.select_one('[class*="bmportalrequirementdetails"]'))
            if el:
                job["description"] = el.get_text(" ", strip=True)[:_DESC_MAX]
        except Exception:
            pass
    # Generic fallback: any job with a detail URL whose ATS-specific branch
    # didn't yield a body (SuccessFactors career sites whose slug is unknown,
    # custom boards, Workday rows that arrived without _wd). Covers the
    # empty-description rows that were silently unscorable.
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
    nothing downstream could score or rank."""
    try:
        r = SESSION.get(url, headers=HEADERS,
                        allow_redirects=True)
        if r.status_code in (403, 405):
            # WAFs (iCIMS) that reject a Chrome UA without Chrome's
            # client-hint headers accept a plain platform UA, the same
            # quirk fetchers/icims.py works around.
            r = SESSION.get(url, allow_redirects=True, headers=icims.ICIMS_HEADERS)
        html = r.text
    except Exception:
        return "", ""
    title = desc = ""
    try:
        from .jsonld import extract_jsonld, is_jobposting, _normalize_description
        for obj in extract_jsonld(html):
            if is_jobposting(obj):
                title = str(obj.get("title") or obj.get("name") or "").strip()
                d = _normalize_description(obj).strip()
                if len(d) >= 120:
                    desc = d[:_DESC_MAX]
                break
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


# --- open/closed probing --------------------------------------------------- #
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


def probe_job_open(url):
    """Best-effort liveness check of one job's own detail URL.

    Returns (is_open, reason): True = positively live, False = positively
    closed, None = indeterminate (bot-gated host, fetch error, or a 200 with
    no recognizable signal); callers must leave stored status alone on None.
    Only used for rows the crawl's board-diff can't cover (see
    tracks.local_tech.check_closed_jobs); board snapshots are authoritative
    where available.
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
                return None, f"workday cxs fetch error: {e}"
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

    try:
        r = SESSION.get(url, headers=HEADERS, allow_redirects=True)
    except Exception as e:
        return None, f"fetch error: {e}"
    if r.status_code in (404, 410):
        return False, f"HTTP {r.status_code}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
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
    return None, "no closed signal"


# --- custom (self-hosted) careers-board scraping -------------------------- #
# A job-detail URL is /careers|jobs|positions|openings|roles|job/<slug>. But
# index/nav pages share that shape ("/careers/open-positions"), so we exclude
# generic slugs and nav-ish link text, and require a *specific* slug.
_JOB_HREF_RE = re.compile(r"/(careers?|jobs?|positions?|openings?|roles?|job)/"
                          r"([a-z0-9][a-z0-9\-_/]{2,})", re.I)
_NAV_SLUGS = {
    "open-positions", "open-roles", "career-opportunities", "current-openings",
    "job-openings", "openings", "opportunities", "jobs", "job", "careers",
    "career", "apply", "application", "search", "all", "browse", "students",
    "internships", "benefits", "culture", "life", "teams", "team", "departments",
    "locations", "faq", "contact", "index", "home", "overview",
}
_NAV_TEXT_RE = re.compile(
    r"^(careers?|jobs?|view (all|current|open)|open (positions?|roles?)|"
    r"see (all|open)|apply|search|browse|all (jobs|openings|roles)|"
    r"current openings|open positions|view (job )?openings|join( us)?|"
    r"work (with|at) us|learn more|explore|opportunities|all roles)\b", re.I)
_OPENINGS_HREF_RE = re.compile(
    r"/(open-positions|open-roles|career-opportunities|current-openings|"
    r"job-openings|openings|opportunities|positions|jobs)\b", re.I)
# scheme+host extractor and the openings link-text cue, precompiled once
# rather than rebuilt per anchor (the host check was an rf-string with
# re.escape(host), a fresh pattern per distinct host that thrashed re's cache).
_SCHEME_HOST_RE = re.compile(r"https?://([^/]+)")
_OPENINGS_TEXT_RE = re.compile(
    r"(current|open|view|see|all).{0,12}(opening|position|role|job)", re.I)


def find_job_links(soup):
    """Real job-posting links on a careers page (nav / index links filtered)."""
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = _JOB_HREF_RE.search(a["href"])
        if not m:
            continue
        slug = m.group(2).rstrip("/").split("/")[-1].split("?")[0].lower()
        if slug in _NAV_SLUGS or len(slug) < 4:
            continue
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 4 or _NAV_TEXT_RE.match(text):
            continue
        if a["href"] in seen:
            continue
        seen.add(a["href"])
        # Prefer a heading/title element for a clean title (some boards nest
        # the title + location in one <a>); fall back to the full link text.
        te = a.find(["h1", "h2", "h3", "h4", "h5"]) or a.select_one("[class*='title']")
        title = te.get_text(" ", strip=True) if te else text
        out.append((a, a["href"], title))
    return out


# Job aggregators / ATS hosts: never treat as a company's own custom board
# (aggregators are handled by external ingestion; ATS hosts by the sniffer).
_OFFSITE_RE = re.compile(
    r"indeed|linkedin|glassdoor|ziprecruiter|simplyhired|monster|dice|"
    r"greenhouse|lever\.co|ashbyhq|myworkdayjobs|smartrecruiters|icims|"
    r"paylocity|bamboohr|jobvite|google\.com|builtin", re.I)


def _openings_link(soup, root):
    """A SAME-HOST 'see current openings' link to follow one hop, or None.
    Won't follow off to an aggregator or an ATS: those aren't a custom board."""
    host = re.match(r"https?://([^/]+)", root).group(1)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http"):
            absu = href
        elif href.startswith("/"):
            absu = root + href
        else:
            absu = root + "/" + href
        hm = _SCHEME_HOST_RE.match(absu)
        if not hm or hm.group(1) != host:
            continue  # off-domain: skip
        if _OFFSITE_RE.search(absu):
            continue
        text = a.get_text(" ", strip=True).lower()
        if _OPENINGS_HREF_RE.search(href) or _OPENINGS_TEXT_RE.search(text):
            return absu
    return None


def _location_near(a, loc_re=None):
    """
    Best-effort location for a job link: search the link then its container.
    Prefers a loc_re location when the container is multi-location, so a
    role listed "Alameda, CA | Durham, NC" is kept as a Durham job.
    """
    for el in (a, a.parent, a.parent.parent if a.parent else None):
        if el is None:
            continue
        text = el.get_text(" ", strip=True)
        if loc_re is not None:
            m = loc_re.search(text)
            if m:
                return m.group(0)
        m = LOC_TEXT_RE.search(text)
        if m:
            return m.group(0)
    return ""


def _get_soup(url):
    try:
        r = SESSION.get(url, headers=HEADERS)
        if r.status_code != 200:
            return None
        return BeautifulSoup(r.text, "lxml")
    except Exception:
        return None


def _get_anchor_soup(url):
    """Like _get_soup but parses only <a> tags, for callers that just count
    or scan job/openings links (no surrounding-container reads)."""
    try:
        r = SESSION.get(url, headers=HEADERS)
        if r.status_code != 200:
            return None
        return BeautifulSoup(r.text, "lxml", parse_only=_ANCHORS_ONLY)
    except Exception:
        return None


def fetch_custom_careers(careers_url, loc_re=None, _hop=True):
    """
    Scrape a self-hosted / custom careers board (no standard ATS).
    Structure-agnostic: identifies real job-detail links (not nav), reads the
    title from the link and the location from its surrounding container, and
    follows a 'careers -> openings' link one hop when the landing page has no
    postings.
    """
    root = re.match(r"https?://[^/]+", careers_url).group(0)
    soup = _get_soup(careers_url)
    if soup is None:
        return []
    links = find_job_links(soup)
    if len(links) < 3 and _hop:
        op = _openings_link(soup, root)
        if op and op.rstrip("/") != careers_url.rstrip("/"):
            return fetch_custom_careers(op, loc_re, _hop=False)
    out, seen = [], set()
    for a, href, title in links:
        loc = _location_near(a, loc_re)
        if not loc_ok(loc_re, loc):
            continue
        url = href if href.startswith("http") else root + href
        if url in seen:
            continue
        seen.add(url)
        out.append({"id": f"custom_{re.sub(r'[^a-z0-9]+', '-', url.lower())[-48:]}",
                    "title": title[:90], "url": url, "location": loc[:70],
                    "description": "", "ats": "custom", "_wd": None})
    return out


# Short-TTL cache for board-detection results (the hottest app path). The same
# careers URLs are re-checked within a run (sniffer + web-search fallback) and
# across daily runs, each re-check costing a parse + an openings-hop GET. Cache
# the outcome (listing URL, or None for "not a custom board") keyed by page URL.
# TTL is deliberately SHORT so a board that later goes live, or one that goes
# dead, is re-checked within the window rather than pinned by a stale negative.
# Transient fetch failures are NOT cached (only decided outcomes), so a network
# blip never suppresses a real board.
_BOARD_CACHE_TTL = 6 * 3600      # seconds


def _board_cache_path(url):
    return cache_dir("board") / f"{hashlib.sha1(url.encode('utf-8')).hexdigest()}.json"


def _board_cache_get(url):
    """(listing_or_None,) on a live entry, or None on miss/expired/error.
    The 1-tuple lets callers distinguish a cached negative from a miss."""
    p = _board_cache_path(url)
    try:
        if time.time() - p.stat().st_mtime > _BOARD_CACHE_TTL:
            return None
        return (json.loads(p.read_text("utf-8")).get("listing"),)
    except Exception:
        return None


def _board_cache_put(url, listing):
    try:
        p = _board_cache_path(url)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"listing": listing}), encoding="utf-8")
    except Exception:
        pass


def custom_board_listing_url(page_url, html=None):
    """
    If `page_url` (or the openings page it links to, one hop) is a real custom
    job board (>=3 genuine job-detail links, not nav), return the URL that holds
    the listings; else None. Used by the sniffer to detect + resolve the board.
    """
    if _OFFSITE_RE.search(page_url):
        return None  # aggregator/ATS host is never a company's own custom board
    cached = _board_cache_get(page_url)
    if cached is not None:
        return cached[0]
    root = re.match(r"https?://[^/]+", page_url).group(0)
    # Only job/openings anchors are inspected here, so parse <a> tags only.
    soup = (BeautifulSoup(html, "lxml", parse_only=_ANCHORS_ONLY)
            if html is not None else _get_anchor_soup(page_url))
    if soup is None:
        return None  # transient fetch failure: do NOT cache
    result = None
    if len(find_job_links(soup)) >= 3:
        result = page_url
    else:
        op = _openings_link(soup, root)
        if op and op.rstrip("/") != page_url.rstrip("/"):
            s2 = _get_anchor_soup(op)
            if s2 and len(find_job_links(s2)) >= 3:
                result = op
    _board_cache_put(page_url, result)
    return result


def fetch_wpjson_careers_all(base_url, loc_re=None):
    """WordPress "post-filters-archive" careers endpoint:
    {root}/wp-json/post-filters-archive/get-posts?post_type=career.

    For sites whose careers grid AND per-job pages are all JS-rendered, so
    neither fetch_custom_careers (no server-side anchors) nor the JSON-LD
    path sees anything, but the theme's own REST route serves clean JSON.
    The stored URL is the posting's outbound apply link (an Arcoro/BirdDog
    portal page, server-rendered); hydrate_description's "wpjson" branch
    pulls the JD text from it."""
    m = re.match(r"https?://[^/]+", base_url or "")
    if not m:
        return []
    root = m.group(0)
    host = re.sub(r"^https?://(www\.)?", "", root)
    out, page = [], 1
    while True:
        d = _get_json(f"{root}/wp-json/post-filters-archive/get-posts"
                      f"?post_type=career&posts_per_page=100&paged={page}",
                      f"wpjson {host}")
        if not d:
            break
        for p in d.get("posts", []) or []:
            loc_d = p.get("location") or {}
            loc = ", ".join(x for x in (loc_d.get("city"), loc_d.get("state"))
                            if x) or "See posting"
            if not loc_ok(loc_re, loc):
                continue
            url = ((p.get("link") or {}).get("url")) or p.get("permalink") or ""
            out.append({"id": f"wpjson_{host}_{p.get('ID')}",
                        "title": p.get("post_title") or "Unknown",
                        "url": url, "location": loc, "description": "",
                        "posted_at": norm_posted_date((p.get("post_date") or "")[:10]),
                        "ats": "wpjson", "_wd": None})
        if page >= int(d.get("max_num_pages") or 1):
            break
        page += 1
    return out


# --- dispatch ------------------------------------------------------------------ #

# Detail budget for a whole-board pull: the company was vetted, so every
# in-area row is worth its description (the sweep's default is a screening
# budget), paced a little faster than the sweep.
_WHOLE_BOARD = dict(max_details=200, detail_delay=0.15)

# ats -> (store row, loc_re) -> company-shaped jobs. The rows are those of the
# ATS's fetcher module, ungated, with the location filter applied on the
# listing before any detail call (fetchers/board.py).
FETCHERS = {
    "greenhouse":      lambda c, lr: _adapt(fetch_greenhouse(c["slug"], loc_re=lr), "greenhouse"),
    "lever":           lambda c, lr: _adapt(fetch_lever(c["slug"], loc_re=lr), "lever"),
    "ashby":           lambda c, lr: _adapt(fetch_ashby(c["slug"], loc_re=lr), "ashby"),
    "jazzhr":          lambda c, lr: _adapt(fetch_jazzhr("", c["slug"], loc_re=lr), "jazzhr"),
    "jobvite":         lambda c, lr: _adapt(fetch_jobvite(c["slug"], loc_re=lr), "jobvite"),
    "bamboohr":        lambda c, lr: _adapt(fetch_bamboohr(c["slug"], loc_re=lr, **_WHOLE_BOARD), "bamboohr"),
    "adp":             lambda c, lr: _adapt(fetch_adp(*c["slug"].split("|", 1), loc_re=lr, **_WHOLE_BOARD), "adp"),
    "kula":            lambda c, lr: _adapt(fetch_kula("", c["slug"], loc_re=lr), "kula"),
    "paylocity":       lambda c, lr: _adapt(fetch_paylocity(c["slug"], loc_re=lr, **_WHOLE_BOARD), "paylocity"),
    "rippling":        lambda c, lr: _adapt(fetch_rippling(c["slug"], loc_re=lr, **_WHOLE_BOARD), "rippling"),
    "ultipro":         lambda c, lr: _adapt(fetch_ultipro(c["slug"], loc_re=lr), "ultipro"),
    "hibob":           lambda c, lr: _adapt(fetch_hibob(c["slug"], loc_re=lr), "hibob"),
    "workday":         lambda c, lr: _adapt(fetch_workday_all(c["wd_tenant"], c["wd_pod"], c["wd_site"], lr), "workday"),
    "smartrecruiters": lambda c, lr: fetch_smartrecruiters_all(c["slug"], lr),
    "icims":           lambda c, lr: _adapt(fetch_icims_all(c["slug"], lr), "icims"),
    "successfactors":  lambda c, lr: _adapt(fetch_successfactors("", c["careers_url"], loc_re=lr), "successfactors"),
    "peopleadmin":     lambda c, lr: fetch_peopleadmin_all(c["careers_url"], lr),
    "custom":          lambda c, lr: fetch_custom_careers(c["careers_url"], lr),
    "wpjson":          lambda c, lr: fetch_wpjson_careers_all(c["careers_url"], lr),
}


def fetch_company(company, loc_re=None):
    """Dispatch to the right fetcher for a company dict from the store.

    `loc_re=None` pulls the whole board; pass NC_RE for a pull scoped to
    the profile's locality (the local track's default).
    """
    fn = FETCHERS.get(company.get("ats"))
    return fn(company, loc_re) if fn else []


# fetch_company with the profile's locality regex; used by discovery
# (ats_dork, local_sourcing) to sample a board's local postings.
def fetch_company_nc(company):
    return fetch_company(company, NC_RE)
