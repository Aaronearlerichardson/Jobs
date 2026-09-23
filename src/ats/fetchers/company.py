"""
Company-scoped fetching: ALL of a *mission-vetted* company's postings,
optionally location-filtered, in one shape whatever the ATS.

The company was already vetted (mission scored at discovery time, stored in
src/store/__init__.py), so the whole board is pulled with no relevance gate and the
caller's own filter chain decides. `fetch_company` dispatches a store row
to the ATS's fetcher module (fetchers/<ats>.py, the same functions the
unvetted-board sweep calls with `gate=is_relevant`) and `_adapt` puts the
result in the company-fetch shape (`board.adapt`); a platform with a
`config.BOARDS` spec is pulled by its engine, `board.Board.whole_board`:

    {"id", "title", "url", "location", "description", "ats", "_wd",
     ["posted_at"], ["remote_hint"]}

`_wd` is Workday's (tenant, pod, site, path) for `hydrate_description`,
None elsewhere. The module also keeps what has no fetcher module of its
own: SmartRecruiters and WordPress careers endpoints, self-hosted
("custom") careers pages and their board detection, and the per-URL
description/title readers. The per-job open/closed probe that read
those same platforms is fetchers/probe.py.
"""

import re
from urllib.parse import unquote, urldefrag, urljoin

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

from src.net.http import HEADERS, SESSION, fetch_failed, get_json, note_capped
from src.match.locality import NC_RE, location_unknown  # profile [locality]
from src.net.util import (LOC_TEXT_RE, cache_dir, clean_field,
                          default_search_text, hashed_cache_path, host_of,
                          json_cache_get, json_cache_put, norm_posted_date,
                          origin_of, text_from_html)
from . import icims, infor, phenom, workday
from .adp_wfn import fetch_adp
from .bamboohr import fetch_bamboohr
from .board import BOARDS, adapt as _adapt, board_for, loc_ok
from .hibob import fetch_hibob
from .html_scrape import fetch_kula, fetch_successfactors
from .icims import fetch_icims_all
from .infor import fetch_infor_all
from .jazzhr import fetch_jazzhr
from .jobvite import fetch_jobvite
from .paylocity import fetch_paylocity
from .peopleadmin import fetch_peopleadmin
from .phenom import fetch_phenom_all
from .rippling import fetch_rippling
from .ultipro import fetch_ultipro
from .workable import fetch_workable
from .workday import fetch_workday_all, wd_local_count  # noqa: F401 (re-export)

# JD text budget (config.MAX_DESC_CHARS): one cap shared with storage and the
# scoring prompt, so a long posting's requirements block survives end to end.
_DESC_MAX = config.MAX_DESC_CHARS

# Kept for discovery (local_sourcing), which imports the Workday search
# term under this name.
_default_search_text = default_search_text


#: One board's postings, formatted with its slug. SmartRecruiters has no
#: module of its own, so this is the one copy the closure probe, the slug
#: probe and the employer-name check read.
SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies/{}/postings"

# Pages a whole-board SmartRecruiters pull reads by default (x page size
# 100 = the pre-2026-09-18 1,000-row cap). FETCHERS' "smartrecruiters"
# entry below raises this for a mission-worth-it board via
# config.board_max_pages -- see src.config.policy.BOARD_MAX_ROWS.
_SR_MAX_PAGES = 10


def fetch_smartrecruiters_all(slug, loc_re=None, max_pages=_SR_MAX_PAGES):
    """SmartRecruiters public postings API. Descriptions hydrated lazily.

    Reports a capped snapshot (net.http.note_capped) when every page up to
    `max_pages` came back full, or, on an unscoped pull, when fewer rows
    came back than the response's own `totalFound`. A scoped pull's
    `totalFound` is never compared: the rows it drops for locality are not
    missing.

    A page `get_json` cannot read (error status, non-JSON body) ends the
    walk as a reported failure, never as the board's end.
    """
    out = []
    total = None
    for page in range(max_pages):
        data = get_json(f"{SMARTRECRUITERS_API.format(slug)}"
                         f"?limit=100&offset={page*100}",
                         f"smartrecruiters {slug} p{page}")
        if data is None:
            break
        if isinstance(data.get("totalFound"), (int, float)):
            total = data["totalFound"]
        content = data.get("content", []) or []
        if not content:
            break
        for p in content:
            loc = p.get("location", {}) or {}
            # Each COMPONENT cleaned before the join, not the joined
            # string: a trailing tab on "city" would otherwise survive the
            # collapse as a space before the comma ("Durham , NC"), and a
            # component that is only whitespace would contribute a bare ", ".
            loc_s = ", ".join(x for x in (clean_field(loc.get("city")),
                                          clean_field(loc.get("region")),
                                          clean_field(loc.get("country"))) if x)
            if not loc_ok(loc_re, loc_s):
                continue
            pid = p.get("id")
            out.append({"id": f"sr_{slug}_{pid}",
                        "title": clean_field(p.get("name")),
                        "url": f"https://jobs.smartrecruiters.com/{slug}/{pid}",
                        "location": loc_s, "description": "", "ats": "smartrecruiters",
                        "_wd": None, "_sr": (slug, pid),
                        "posted_at": norm_posted_date(p.get("releasedDate"))})
        if len(content) < 100:
            break
    else:
        note_capped(total if loc_re is None else None)
    if loc_re is None and (total or 0) > len(out):
        note_capped(total)
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


def needs_detail(job):
    """True when hydrate_description would fetch anything for `job`: no
    body yet, or (Workday only) a body already but a location the listing
    never resolved -- the "<N> Locations" placeholder (or any other
    location_unknown text). Every OTHER ats's location comes solely from
    the listing (hydrate_description never revisits it once a body is in),
    so a bodied non-Workday row never needs a second detail call. Shared
    by harvest._hydrate_rows and triage._hydrate, which both select rows
    to fetch by this predicate rather than "no description" alone.

    >>> needs_detail({"description": "", "ats": "greenhouse", "_wd": None})
    True
    >>> needs_detail({"description": "d", "ats": "greenhouse", "_wd": None})
    False
    >>> wd = ("acme", 5, "Ext", "/job/x")
    >>> needs_detail({"description": "d", "ats": "workday", "_wd": wd,
    ...               "location": "2 Locations"})
    True
    >>> needs_detail({"description": "d", "ats": "workday", "_wd": wd,
    ...               "location": "Durham, NC"})
    False
    """
    board = board_for(job.get("ats"))
    if board:
        return board.needs_detail(job)
    if not job.get("description"):
        return True
    return job.get("ats") == "workday" and bool(job.get("_wd")) \
        and location_unknown(job.get("location"))


def _hydrate_from_url(platform, url):
    """(description, location), "" each on a miss, for a stored job URL
    on a platform whose detail call needs only what the URL names: the
    module supplies job_ref_from_url, detail, detail_description and
    detail_location (phenom, infor)."""
    ref = platform.job_ref_from_url(url)
    if not ref:
        return "", ""
    payload = platform.detail(*ref)
    return platform.detail_description(payload), platform.detail_location(payload)


def hydrate_description(job):
    """Fetch, in place, whatever `needs_detail` says `job` still lacks: a
    bodiless row's description through its ATS's detail call (for Workday
    the same JSON also carries the location list), or a bodied Workday
    row's location alone, from the disk-cached per-req lookup
    (workday._wd_detail_locations) with no body refetch.
    """
    if not needs_detail(job):
        return job
    board = board_for(job.get("ats"))
    if board:
        board.hydrate(job)
    elif job.get("ats") == "workday" and job.get("_wd"):
        if job.get("description"):
            locs = workday._wd_detail_locations(*job["_wd"])
            if locs:
                job["location"] = "; ".join(locs)
            return job
        info = workday.cxs_detail(*job["_wd"])
        if info:
            job["description"] = workday.text_from_html(
                info.get("jobDescription", ""))[:_DESC_MAX]
            # Same JSON carries the req's full location list: upgrade a
            # useless "2 Locations" locationsText to the real thing.
            locs = workday.detail_locations(info)
            if locs and location_unknown(job.get("location")):
                job["location"] = "; ".join(locs)
        return job
    if job.get("ats") == "smartrecruiters" and job.get("_sr"):
        slug, pid = job["_sr"]
        try:
            r = SESSION.get(f"{SMARTRECRUITERS_API.format(slug)}/{pid}", headers=HEADERS)
            secs = r.json().get("jobAd", {}).get("sections", {}) or {}
            parts = [secs.get(k, {}).get("text", "") for k in
                     ("jobDescription", "qualifications", "additionalInformation")]
            html = " ".join(p for p in parts if p)
            job["description"] = text_from_html(html)[:_DESC_MAX]
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
    elif job.get("ats") == "workable" and job.get("url"):
        # Same URL-only round trip as the rippling branch above: the stored
        # tenant-path URL carries BOTH coordinates (account slug + posting
        # shortcode), and no "_"-prefixed key survives _adapt. The slug-less
        # short link names no account, so job_ref_from_url refuses it and
        # the generic fallback below is what tries.
        from .workable import fetch_description, job_ref_from_url
        ref = job_ref_from_url(job["url"])
        if ref:
            job["description"] = fetch_description(*ref)[:_DESC_MAX]
    elif job.get("ats") == "icims" and job.get("url"):
        # The ?in_iframe=1 document is server-rendered with JSON-LD even on
        # JS-shell tenants; it also names the posting's real location(s).
        loc, desc = icims.job_meta(job["url"], need_desc=True)
        if desc:
            job["description"] = desc[:_DESC_MAX]
        if loc and (job.get("location") or "").strip() in ("", icims.LOCAL_LABEL):
            job["location"] = loc
    elif job.get("ats") == "phenom" and job.get("url"):
        # No "_"-prefixed coordinate survives _adapt for this ATS (only
        # Workday's _wd does), so the detail coordinates are re-derived
        # from the job's own URL -- same pattern as the paylocity/
        # rippling branches above.
        desc, loc = _hydrate_from_url(phenom, job["url"])
        if desc:
            job["description"] = desc[:_DESC_MAX]
        if loc:
            job["location"] = loc
    elif job.get("ats") == "infor" and job.get("url"):
        # Same URL-only round trip as the phenom branch; the generic
        # fallback below cannot help here, the stored URL being a JS shell.
        desc, loc = _hydrate_from_url(infor, job["url"])
        if desc:
            job["description"] = desc[:_DESC_MAX]
        if loc and location_unknown(job.get("location")):
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


def _openings_link(soup, page_url):
    """A SAME-HOST 'see current openings' link to follow one hop, or None.
    Won't follow off to an aggregator or an ATS: those aren't a custom board."""
    host = host_of(page_url)
    if not host:
        return None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Defragmented, so an "#open-positions" link reads as this page and
        # the callers' no-self-hop check refuses it.
        absu = urldefrag(urljoin(page_url, href)).url
        if host_of(absu) != host:
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
    """GET `url` and parse it; None on a fetch exception or non-200
    status, reported via fetch_failed: this is fetch_custom_careers'
    board-listing fetch, where a silent 404 (a stale careers_url) read as
    an empty board. Discovery probes use the silent `_get_anchor_soup`."""
    try:
        r = SESSION.get(url, headers=HEADERS)
    except Exception as e:
        fetch_failed(f"custom careers {url}", e)
        return None
    if r.status_code != 200:
        fetch_failed(f"custom careers {url}", f"HTTP {r.status_code}")
        return None
    return BeautifulSoup(r.text, "lxml")


def _get_anchor_soup(url):
    """Like _get_soup but parses only <a> tags, for callers that just count
    or scan job/openings links (no surrounding-container reads). Silent: a
    probed page that is not a board is an expected answer."""
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
    soup = _get_soup(careers_url)
    if soup is None:
        return []
    links = find_job_links(soup)
    if len(links) < 3 and _hop:
        op = _openings_link(soup, careers_url)
        if op and op.rstrip("/") != careers_url.rstrip("/"):
            return fetch_custom_careers(op, loc_re, _hop=False)
    out, seen = [], set()
    for a, href, title in links:
        loc = clean_field(_location_near(a, loc_re))
        if not loc_ok(loc_re, loc):
            continue
        url = urljoin(careers_url, href)
        if url in seen:
            continue
        seen.add(url)
        out.append({"id": f"custom_{re.sub(r'[^a-z0-9]+', '-', url.lower())[-48:]}",
                    "title": clean_field(title)[:90], "url": url,
                    "location": loc[:70],
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
    return hashed_cache_path(cache_dir("board"), url)


def _board_cache_get(url):
    """(listing_or_None,) on a live entry, or None on miss/expired/error.
    The 1-tuple lets callers distinguish a cached negative from a miss."""
    cached = json_cache_get(_board_cache_path(url), _BOARD_CACHE_TTL)
    return None if cached is None else (cached.get("listing"),)


def _board_cache_put(url, listing):
    json_cache_put(_board_cache_path(url), {"listing": listing})


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
    # Only job/openings anchors are inspected here, so parse <a> tags only.
    soup = (BeautifulSoup(html, "lxml", parse_only=_ANCHORS_ONLY)
            if html is not None else _get_anchor_soup(page_url))
    if soup is None:
        return None  # transient fetch failure: do NOT cache
    result = None
    if len(find_job_links(soup)) >= 3:
        result = page_url
    else:
        op = _openings_link(soup, page_url)
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
    root = origin_of(base_url)
    if not root:
        return []
    host = re.sub(r"^https?://(www\.)?", "", root)
    out, page = [], 1
    while True:
        d = get_json(f"{root}/wp-json/post-filters-archive/get-posts"
                      f"?post_type=career&posts_per_page=100&paged={page}",
                      f"wpjson {host}")
        if not d:
            break
        for p in d.get("posts", []) or []:
            loc_d = p.get("location") or {}
            loc = ", ".join(x for x in (clean_field(loc_d.get("city")),
                                        clean_field(loc_d.get("state")))
                            if x) or "See posting"
            if not loc_ok(loc_re, loc):
                continue
            url = ((p.get("link") or {}).get("url")) or p.get("permalink") or ""
            out.append({"id": f"wpjson_{host}_{p.get('ID')}",
                        "title": clean_field(p.get("post_title")) or "Unknown",
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
_WHOLE_BOARD = dict(max_details=config.WHOLE_BOARD_DETAILS,
                    detail_delay=config.WHOLE_BOARD_DETAIL_DELAY_S)

# ats -> (store row, loc_re) -> company-shaped jobs. The rows are those of the
# ATS's fetcher module, ungated, with the location filter applied on the
# listing before any detail call (fetchers/board.py).
FETCHERS = {
    **{b.name: b.whole_board for b in BOARDS.values() if b.fetchable},
    "jazzhr":          lambda c, lr: _adapt(fetch_jazzhr("", c["slug"], loc_re=lr), "jazzhr"),
    "jobvite":         lambda c, lr: _adapt(fetch_jobvite(c["slug"], loc_re=lr), "jobvite"),
    "bamboohr":        lambda c, lr: _adapt(fetch_bamboohr(c["slug"], loc_re=lr, **_WHOLE_BOARD), "bamboohr"),
    "adp":             lambda c, lr: _adapt(fetch_adp(*c["slug"].split("|", 1), loc_re=lr, **_WHOLE_BOARD), "adp"),
    "kula":            lambda c, lr: _adapt(fetch_kula("", c["slug"], loc_re=lr), "kula"),
    "paylocity":       lambda c, lr: _adapt(fetch_paylocity(c["slug"], loc_re=lr, **_WHOLE_BOARD), "paylocity"),
    "rippling":        lambda c, lr: _adapt(fetch_rippling(c["slug"], loc_re=lr, **_WHOLE_BOARD), "rippling"),
    "ultipro":         lambda c, lr: _adapt(fetch_ultipro(c["slug"], loc_re=lr), "ultipro"),
    "hibob":           lambda c, lr: _adapt(fetch_hibob(c["slug"], loc_re=lr), "hibob"),
    "workable":        lambda c, lr: _adapt(fetch_workable(c["slug"], loc_re=lr, **_WHOLE_BOARD), "workable"),
    # Page budget: config.board_max_pages raises it for a mission-worth-it
    # board (config.BOARD_MAX_ROWS), else keeps the fetcher's own narrower
    # default (workday._WD_MAX_PAGES / _SR_MAX_PAGES) for one
    # config.is_offmission_inactive -- see policy.board_max_pages.
    "workday":         lambda c, lr: _adapt(fetch_workday_all(
                           c["wd_tenant"], c["wd_pod"], c["wd_site"], lr,
                           max_pages=config.board_max_pages(
                               c, 20, workday._WD_MAX_PAGES)), "workday"),
    "phenom":          lambda c, lr: _adapt(fetch_phenom_all(c.get("slug") or c.get("careers_url"), lr), "phenom"),
    "infor":           lambda c, lr: _adapt(fetch_infor_all(c["slug"], lr), "infor"),
    "smartrecruiters": lambda c, lr: fetch_smartrecruiters_all(
                           c["slug"], lr,
                           max_pages=config.board_max_pages(c, 100, _SR_MAX_PAGES)),
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


# --- title sampling ------------------------------------------------------------ #

# ats -> (store row, n) -> job dicts, for the families whose FETCHERS entry
# pays a request per posting or per page. A title sample wants the first
# listing and nothing else, so these are the same fetchers with the detail
# budget at zero and the pager at one page (or `n` postings, where the
# listing IS the postings). An ATS not named here answers a listing in one
# request and samples through FETCHERS unchanged.
_TITLE_SAMPLERS = {
    "bamboohr":        lambda c, n: fetch_bamboohr(c["slug"], max_details=0),
    "adp":             lambda c, n: fetch_adp(*c["slug"].split("|", 1),
                                              max_details=0, max_pages=1),
    "paylocity":       lambda c, n: fetch_paylocity(c["slug"], max_details=0),
    "rippling":        lambda c, n: fetch_rippling(c["slug"], max_details=0),
    "workable":        lambda c, n: fetch_workable(c["slug"], max_details=0),
    "jobvite":         lambda c, n: fetch_jobvite(c["slug"], max_details=0),
    "jazzhr":          lambda c, n: fetch_jazzhr("", c["slug"], max_jobs=n),
    "icims":           lambda c, n: fetch_icims_all(c["slug"], meta_cap=0),
    "phenom":          lambda c, n: fetch_phenom_all(
                           c.get("slug") or c.get("careers_url"), max_pages=1),
    "infor":           lambda c, n: fetch_infor_all(c["slug"], page_size=n,
                                                    max_pages=1),
    "successfactors":  lambda c, n: fetch_successfactors(
                           "", c["careers_url"], max_pages=1),
    "smartrecruiters": lambda c, n: fetch_smartrecruiters_all(c["slug"], max_pages=1),
    "workday":         lambda c, n: fetch_workday_all(
                           c["wd_tenant"], c["wd_pod"], c["wd_site"], max_pages=1),
}


def sample_titles(company, n=6):
    """Up to `n` distinct posting titles from a store row's board, in board
    order: what the mission scorer is shown of an employer it has only a
    name for. [] when the board is unreadable, empty, or of an ATS with no
    fetcher; never raises.

    `company` carries the store's board columns (`src.ats.coords.columns`;
    `from_hit` turns a resolver hit into them). The pull is listing-only: no
    description, detail or location-rescue request is spent on a sample.

    Notes:
        Until 2026-09-18 the sampler lived in src.discovery.local_sourcing
        with hand-written requests for four ATS families and fell through to
        [] for the other sixteen, 35% of the roster's boards: a company on
        one was mission-scored from its name alone. "Studycast" (Rippling
        board core-sound-imaging, a medical-imaging vendor's PACS product)
        came back `other` / 0.05 as a study-education platform.
    """
    ats = company.get("ats")
    board = board_for(ats)
    try:
        if board:
            handle = board.handle(company)
            jobs = board.listing(handle, cheap=True) if handle else []
        elif ats in _TITLE_SAMPLERS:
            jobs = _TITLE_SAMPLERS[ats](company, n)
        else:
            jobs = fetch_company(company)
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
