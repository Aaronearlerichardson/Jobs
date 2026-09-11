"""Workday career sites: the CXS JSON listing and the per-req detail.

Workday serves both the job list and each req's full description as plain
JSON under /wday/cxs/, no JavaScript needed:
  POST {host}/wday/cxs/{tenant}/{site}/jobs            -> listing
  GET  {host}/wday/cxs/{tenant}/{site}{externalPath}   -> one req, incl. body

Two entry points read a board. `fetch_workday_all` is the listing (title,
location and path only), locality-scoped server-side when a `loc_re` is
given; the company-vetted path (fetchers/company.py) takes those rows as
they are and hydrates descriptions later from the `_wd` coordinates each
row carries. `fetch_workday` runs the same rows through the shared board
driver (fetchers/board.py) for the sweep, fetching a description where
the driver asks for one. `fetch_workday_description` serves rows that are
already stored; the op that sweeps it over the whole store lives with the
other backfills, in src/ops/maintenance.py -- here it was the one backfill
that could not reach `track_store`, and so ran against the default DB.
"""

import hashlib
import html
import json
import re
import time
from urllib.parse import urlparse

from src import config
from src.net.http import SESSION, JSON_HEADERS
from src.net.util import cache_dir, default_search_text, norm_posted_date
from .board import board_jobs, loc_ok

_CXS_HEADERS = {**JSON_HEADERS, "Content-Type": "application/json"}

# Multi-location rows list as literally "2 Locations" / "12 Locations" in
# locationsText, a string no locality regex can match, which silently
# dropped most multi-city reqs (a Durham+Santa Clara posting, a "Firmware
# Engineer, Durham" plus one more site). Those rows are rescued via the
# externalPath location slug and, failing that, the CXS detail's full
# location list (cached on disk; locations rarely change).
N_LOCATIONS_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)
_LOC_CACHE_TTL = 3 * 24 * 3600

# Detail GETs a single board pull may spend expanding "N Locations" rows.
# Each is a request plus the politeness pause; a scoped board needing more
# than this has a scope that failed (caught by _wd_scope_failed) or is a
# multi-site conglomerate whose remaining rows are not worth the wait.
_WD_RESCUE_CAP = 150
# Pages a board pull reads at most (x page_size 20 = the 1,200-row cap the
# funnel shows for the largest boards). A scoped total at or past it is one
# the scope never narrowed.
_WD_MAX_PAGES = 60


def text_from_html(raw):
    """Strip a Workday jobDescription HTML blob to readable plain text."""
    if not raw:
        return ""
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    txt = re.sub(r"(?i)<(/p|/li|/h[1-6]|br\s*/?|/div)\s*>", "\n", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n\n", txt)
    return txt.strip()


# --- the CXS tenant id ----------------------------------------------------- #
# The /wday/cxs/{tenant}/ PATH segment is the internal tenant id, which for
# hyphenated subdomains is usually the UNDERSCORE form: vhr-unither.wd5's
# jobs endpoint is /wday/cxs/vhr_unither/External/jobs, and the hyphen form
# 422s. Observed live in the browser's own network traffic. A board pull
# resolves it once per tenant per process (_wd_cxs_tenant); a stored job
# URL, which names no site coordinates, tries both forms per request
# (_cxs_tenant_variants).
_WD_CXS_TENANT = {}


def _wd_cxs_tenant(tenant, pod, site):
    """The tenant id that works in this board's /wday/cxs/ path."""
    key = (tenant, pod, site)
    if key in _WD_CXS_TENANT:
        return _WD_CXS_TENANT[key]
    resolved = tenant
    variants = [tenant] + ([tenant.replace("-", "_")] if "-" in tenant else [])
    for cand in variants:
        try:
            r = SESSION.post(
                f"https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{cand}/{site}/jobs",
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                headers=_CXS_HEADERS)
            if r.status_code == 200 and isinstance(r.json().get("total"), int):
                resolved = cand
                break
        except Exception:
            continue
    _WD_CXS_TENANT[key] = resolved
    return resolved


def _cxs_detail_url(job_url):
    """Map a Workday job-page URL to its CXS JSON detail endpoint, or None.

    https://{tenant}.wd{N}.myworkdayjobs.com[/en-US]/{site}/job/{path}
      -> https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{path}
    """
    u = urlparse(job_url)
    if "myworkdayjobs.com" not in u.netloc or ".wd" not in u.netloc:
        return None
    host = f"{u.scheme}://{u.netloc}"
    tenant = u.netloc.split(".wd", 1)[0]
    parts = [p for p in u.path.split("/") if p]
    if parts and parts[0].lower() in ("en-us", "en"):
        parts = parts[1:]
    if "job" not in parts or parts.index("job") == 0:
        return None
    site = parts[0]
    rest = "/".join(parts[parts.index("job"):])
    return f"{host}/wday/cxs/{tenant}/{site}/{rest}"


def _cxs_tenant_variants(detail_url):
    """The CXS URL as-is plus, for hyphenated tenants, the underscore-tenant
    form. Applies only to the /wday/cxs/<tenant>/ segment: the HOST keeps
    its hyphen."""
    urls = [detail_url]
    m = re.match(r"(https://([^./]+)\.[^/]+/wday/cxs/)([^/]+)(/.*)", detail_url)
    if m and "-" in m.group(3):
        urls.append(f"{m.group(1)}{m.group(3).replace('-', '_')}{m.group(4)}")
    return urls


def _cxs_description(detail_url, timeout=None):
    """GET a CXS job-detail endpoint; return (plain_text_description, remoteType)."""
    for url in _cxs_tenant_variants(detail_url):
        try:
            r = SESSION.get(url, timeout=timeout, headers=JSON_HEADERS)
            if r.status_code != 200:
                continue
            info = r.json().get("jobPostingInfo", {}) or {}
        except Exception:
            continue
        return text_from_html(info.get("jobDescription", "")), info.get("remoteType")
    return None, None


def fetch_workday_description(job_url):
    """Public: full JD text for one stored Workday job URL (None on failure)."""
    detail = _cxs_detail_url(job_url)
    if not detail:
        return None
    text, _remote = _cxs_description(detail)
    return text or None


# --- one req's detail, by board coordinates -------------------------------- #

def cxs_detail(tenant, pod, site, path):
    """The `jobPostingInfo` JSON of one req (its body, location list, ...),
    or {} on any failure. `path` is the listing's externalPath, which
    already starts with "/job/": the detail URL is {site}{path}, NOT
    {site}/job{path} (the doubled form 406s)."""
    api = (f"https://{tenant}.wd{pod}.myworkdayjobs.com"
           f"/wday/cxs/{_wd_cxs_tenant(tenant, pod, site)}/{site}{path}")
    try:
        r = SESSION.get(api, headers=JSON_HEADERS)
        if r.status_code != 200:
            return {}
        return r.json().get("jobPostingInfo", {}) or {}
    except Exception:
        return {}


def detail_locations(info):
    """Every location a req's detail JSON names: primary + additionalLocations."""
    locs = [info.get("location", "")] + list(info.get("additionalLocations", []) or [])
    return [l for l in locs if l]


def _wd_path_location(path):
    """The human-readable location slug Workday embeds in externalPath:
    '/job/US-NC-Remote/Field-Application-Engineer_JR123' -> 'US NC Remote'."""
    m = re.match(r"/job/([^/]+)/", path or "")
    return m.group(1).replace("-", " ") if m else ""


def _wd_detail_locations(tenant, pod, site, path):
    """All locations of one req from its CXS detail JSON, disk-cached.
    Returns a list of location strings ([] on any failure: callers must
    treat that as 'unknown', not 'no')."""
    cache = cache_dir("wdloc")
    key = hashlib.sha1(f"{tenant}|{path}".encode("utf-8")).hexdigest()
    p = cache / f"{key}.json"
    try:
        if time.time() - p.stat().st_mtime <= _LOC_CACHE_TTL:
            return json.loads(p.read_text("utf-8")).get("locs", [])
    except Exception:
        pass
    locs = detail_locations(cxs_detail(tenant, pod, site, path))
    if locs:  # cache only decided outcomes, like the board cache
        try:
            cache.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"locs": locs}), encoding="utf-8")
        except Exception:
            pass
    return locs


# --- server-side locality scope -------------------------------------------- #

def _wd_facets_and_total(api, hdr, loc_re):
    """(appliedFacets for `loc_re`, the board's UNSCOPED posting total) from
    one page-0 call.

    The facets are the server-side location filter: every location-
    flavored facet value whose descriptor matches loc_re, grouped by the
    facetParameter that owns it ('locations' on most tenants). {} when the
    tenant exposes none, and callers fall back to free-text search. Solves
    tenants whose searchText ignores state names ('North Carolina' -> 2
    hits vs 191 real NC reqs across 46 city facet values).

    The total is None when the call fails; it is what lets a scoped pull
    notice that its scope did nothing (see _wd_scope_failed)."""
    try:
        r = SESSION.post(api, json={"appliedFacets": {}, "limit": 1,
                                    "offset": 0, "searchText": ""}, headers=hdr)
        data = r.json()
        facets = data.get("facets", []) or []
        total = data.get("total")
        total = int(total) if isinstance(total, (int, float)) else None
    except Exception:
        return {}, None

    applied = {}

    def walk(values, param):
        for v in values or []:
            p = v.get("facetParameter") or param
            if v.get("id") and loc_re.search(v.get("descriptor") or ""):
                applied.setdefault(p, []).append(v["id"])
            walk(v.get("values"), p)

    for f in facets:
        if re.search(r"location|country|region|city|state",
                     f.get("facetParameter") or "", re.I):
            walk(f.get("values"), f.get("facetParameter"))
    return applied, total


def _wd_scope_failed(scoped_total, board_total, cap):
    """Whether a locality-scoped Workday listing came back unnarrowed.

    A scope that returns as many postings as the whole board, or at least
    `cap` (the most the pager will ever read), did nothing: the tenant
    ignored the facet or the search text. 2026-09-09: one board answered
    every scoped call with all 2000 reqs for a day; the pull read 60
    pages, detail-fetched 1,199 "N Locations" rows to rescue them (531s
    of an 872s crawl), and kept all 1,200 as local.

    >>> _wd_scope_failed(82, 2000, 1200)
    False
    >>> _wd_scope_failed(2000, 2000, 1200)
    True
    >>> _wd_scope_failed(1300, None, 1200)
    True
    >>> _wd_scope_failed(None, 2000, 1200)
    False
    >>> _wd_scope_failed(0, 0, 1200)
    False
    """
    if not isinstance(scoped_total, (int, float)) or scoped_total <= 0:
        return False
    if isinstance(board_total, (int, float)) and board_total > 0 \
            and scoped_total >= board_total:
        return True
    return scoped_total >= cap


def _scoped_body(api, hdr, loc_re, search_text):
    """(request body for the listing POSTs, appliedFacets used, board total)
    for a pull scoped to `loc_re`; an unscoped body when loc_re is None."""
    applied, board_total = {}, None
    if loc_re is not None:
        applied, board_total = _wd_facets_and_total(api, hdr, loc_re)
    # Never send a location term on a whole-board pull: loc_re=None IS the
    # request for everything, and narrowing it anyway silently hid every
    # out-of-area posting from callers that asked for the full board.
    if search_text is None:
        search_text = default_search_text() if loc_re is not None else ""
    body = ({"appliedFacets": applied, "searchText": ""} if applied
            else {"appliedFacets": {}, "searchText": search_text})
    return body, applied, board_total


def fetch_workday_all(tenant, pod, site, loc_re=None, search_text=None,
                      page_size=20, max_pages=_WD_MAX_PAGES):
    """List postings (title/location/path only; descriptions come later).

    Location scoping, most-precise first:
      1. loc_re given and the tenant exposes location facets -> appliedFacets
         (server-side, catches multi-location reqs the text filter can't).
      2. loc_re given, no usable facets -> `search_text` free-text narrowing
         (legacy behavior), still with the multi-location rescue below.
      3. loc_re None -> the WHOLE board, unnarrowed.
    In cases 1 and 2, a row whose locationsText fails loc_re gets two more
    chances: the externalPath location slug, then the CXS detail's full
    location list ("2 Locations" rows). Rescued rows carry the REAL joined
    location string so downstream geo logic sees the evidence.

    `search_text` defaults to a term derived from the profile's [locality];
    pass "" for an explicitly unnarrowed pull.

    Each row carries `_wd` = (tenant, pod, site, externalPath), the
    coordinates `cxs_detail` needs to fetch its body later.
    """
    host = f"https://{tenant}.wd{pod}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{_wd_cxs_tenant(tenant, pod, site)}/{site}/jobs"
    link = f"{host}/en-US/{site}"
    body_extra, applied_facets, board_total = _scoped_body(
        api, _CXS_HEADERS, loc_re, search_text)

    out = []
    scope_failed = False   # the scope came back unnarrowed (see below)
    rescues = 0            # detail GETs spent on "N Locations" rows
    for page in range(max_pages):
        try:
            r = SESSION.post(api, json={**body_extra, "limit": page_size,
                                        "offset": page * page_size},
                             headers=_CXS_HEADERS)
            data = r.json()
            posts = data.get("jobPostings", []) or []
        except Exception as e:
            print(f"    [!] workday {tenant} p{page}: {e}")
            break
        if not posts:
            break
        if page == 0 and loc_re is not None and _wd_scope_failed(
                data.get("total"), board_total, page_size * max_pages):
            # The tenant ignored the facet/search text: every row is here,
            # not just the local ones, so the facet no longer vouches for
            # anything and a per-row detail rescue would GET the whole
            # board. Keep only rows whose LISTED location matches.
            scope_failed = True
            print(f"    [!] workday {tenant}: locality scope came back "
                  f"unnarrowed ({data.get('total')} of {board_total or '?'} "
                  f"postings) - keeping listed-location matches only, no "
                  f"detail rescue")
        for p in posts:
            loc = p.get("locationsText", "") or ""
            path = p.get("externalPath", "") or ""
            if scope_failed:
                slug_loc = _wd_path_location(path)
                if loc_ok(loc_re, loc):
                    pass
                elif loc_ok(loc_re, slug_loc):
                    loc = f"{slug_loc}" + (f" ({loc})" if loc else "")
                else:
                    continue
            elif not loc_ok(loc_re, loc):
                # Rescue 1: the externalPath's location slug (free).
                slug_loc = _wd_path_location(path)
                if loc_ok(loc_re, slug_loc):
                    loc = f"{slug_loc}" + (f" ({loc})" if loc else "")
                # Rescue 2: "N Locations" rows, full list via CXS detail.
                elif N_LOCATIONS_RE.match(loc) and path:
                    if rescues >= _WD_RESCUE_CAP:
                        # Budget spent. A facet-scoped listing vouched the
                        # row is in-area: keep it on its listed text. A
                        # search-text one did not: unknown stays out.
                        if not applied_facets:
                            continue
                    else:
                        rescues += 1
                        locs = _wd_detail_locations(tenant, pod, site, path)
                        if not any(loc_ok(loc_re, l) for l in locs):
                            continue
                        loc = "; ".join(locs)
                else:
                    continue
            elif (N_LOCATIONS_RE.match(loc) and path
                  and rescues < _WD_RESCUE_CAP):
                # Facet-filtered fetch already vouches this req is in-area,
                # but "2 Locations" is useless downstream (geo_mode, ranking
                # location filters): resolve the real list.
                rescues += 1
                locs = _wd_detail_locations(tenant, pod, site, path)
                if locs:
                    loc = "; ".join(locs)
            jid = path.rsplit("/", 1)[-1] if path else str(abs(hash(p.get("title", "") + loc)))
            out.append({"id": f"wd_{tenant}_{jid}", "title": p.get("title", ""),
                        "url": f"{link}{path}" if path else host, "location": loc,
                        "description": "",
                        "_wd": (tenant, pod, site, path),
                        # relative text ("Posted 30+ Days Ago"): approximate
                        "posted_at": norm_posted_date(p.get("postedOnDate")
                                                      or p.get("postedOn"))})
        if len(posts) < page_size:
            break
    if rescues >= _WD_RESCUE_CAP:
        print(f"    [!] workday {tenant}: \"N Locations\" detail budget "
              f"({_WD_RESCUE_CAP}) spent; later multi-site rows "
              f"{'kept unexpanded' if applied_facets else 'dropped'}")
    return out


def fetch_workday(tenant, pod, site, company_name="", gate=None, loc_re=None,
                  search_text=None, page_size=20, max_pages=_WD_MAX_PAGES,
                  max_details=_WD_RESCUE_CAP, detail_delay=0.3):
    """`fetch_workday_all`'s rows through the board driver: `loc_re` scopes
    the listing server-side, `gate` screens titles and then, within
    `max_details` CXS GETs, descriptions (see fetchers/board.py)."""
    rows = fetch_workday_all(tenant, pod, site, loc_re=loc_re,
                             search_text=search_text, page_size=page_size,
                             max_pages=max_pages)
    return board_jobs(
        rows, company_name, gate=gate,
        fetch_description=lambda row: text_from_html(
            cxs_detail(*row["_wd"]).get("jobDescription", "")),
        max_details=max_details, detail_delay=detail_delay)


def wd_local_count(tenant, pod, site, loc_re, search_text=None, page_size=20,
                   sample_pages=5):
    """How many of a Workday board's postings are in `loc_re`'s area, the
    way fetch_workday_all would scope them: the facet-scoped (else
    search-text-scoped) total when the scope narrowed the board, otherwise
    a count over the first `sample_pages` pages by LISTED location only.

    The discovery probe used to trust the scoped total outright, and wrote
    local_job_count = total_job_count = 1200 for a board on 2026-09-09
    when the tenant ignored its search text (the crawl then pulled the
    whole board). Returns 0 when the board cannot be read at all."""
    host = f"https://{tenant}.wd{pod}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{_wd_cxs_tenant(tenant, pod, site)}/{site}/jobs"
    body, _applied, board_total = _scoped_body(api, _CXS_HEADERS, loc_re, search_text)
    try:
        r = SESSION.post(api, json={**body, "limit": 1, "offset": 0},
                         timeout=config.PROBE_TIMEOUT, headers=_CXS_HEADERS)
        scoped = int(r.json().get("total", 0) or 0)
    except Exception:
        return 0
    if not _wd_scope_failed(scoped, board_total, page_size * _WD_MAX_PAGES):
        return scoped
    n = 0
    for page in range(sample_pages):
        try:
            r = SESSION.post(api, json={"appliedFacets": {}, "searchText": "",
                                        "limit": page_size,
                                        "offset": page * page_size},
                             timeout=config.PROBE_TIMEOUT, headers=_CXS_HEADERS)
            posts = r.json().get("jobPostings", []) or []
        except Exception:
            break
        for p in posts:
            loc = p.get("locationsText", "") or ""
            if loc_ok(loc_re, loc) or loc_ok(
                    loc_re, _wd_path_location(p.get("externalPath", "") or "")):
                n += 1
        if len(posts) < page_size:
            break
    return n
