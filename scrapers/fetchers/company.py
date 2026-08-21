"""
Company-scoped fetchers: pull ALL of a *mission-vetted* company's postings,
optionally location-filtered.

Contrast with the sibling modules in scrapers/fetchers/* — those pre-filter
every posting through the keyword filter (filters.is_relevant), which is right
for sweeping unvetted boards. Here the company was already vetted (mission
scored at discovery time, stored in core/store.py), so the whole board
is pulled and the caller's own filter chain decides.
"""

import hashlib
import json
import re
import time

import config
from bs4 import BeautifulSoup, SoupStrainer

# Parse pages with lxml (2-3x faster than html.parser, and the gap widens with
# page size). For paths that only need job/nav anchors — link counting and the
# openings-hop — restrict parsing to <a> tags via SoupStrainer; anchors and
# their descendants are preserved (find_job_links reads a title element inside
# each <a>), which is all those callers touch. Paths that read an anchor's
# surrounding container (location extraction in fetch_custom_careers) keep the
# full tree via _get_soup.
_ANCHORS_ONLY = SoupStrainer("a")

from ..http import HEADERS, SESSION
from core.locality import NC_RE, location_snippet  # profile [locality]: the location gate
from ..util import norm_posted_date

# JD text budget (config.MAX_DESC_CHARS): one cap shared with storage and the
# scoring prompt, so a long posting's requirements block survives end to end.
_DESC_MAX = config.MAX_DESC_CHARS


def _loc_ok(loc_re, text):
    return loc_re is None or bool(loc_re.search(text or ""))


def _get_json(url, label, **kw):
    """GET + parse JSON, treating any HTTP error, empty body, or non-JSON
    response as a clean miss (returns None) rather than an exception that
    surfaces as a cryptic ``Expecting value`` further up the stack."""
    r = SESSION.get(url, timeout=25, headers=HEADERS, **kw)
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


def _merge_locations(primary, extras):
    """One location string carrying every location a posting names: the
    primary field first, then any secondary office/location not already
    present in it. Multi-location postings often show only 'Remote' (or one
    HQ city) in the primary field while the NC site hides in the secondary
    list — the location filter and geo logic must see them all."""
    loc = (primary or "").strip()
    seen = loc.lower()
    for e in extras or []:
        e = (e or "").strip()
        if e and e.lower() not in seen:
            loc = f"{loc}; {e}" if loc else e
            seen = loc.lower()
    return loc


def fetch_greenhouse_all(slug, loc_re=None):
    out = []
    try:
        data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                         f"greenhouse {slug}")
        if not isinstance(data, dict):
            return out
        for j in data.get("jobs", []):
            loc = _merge_locations(
                j.get("location", {}).get("name", ""),
                [o.get("name", "") for o in (j.get("offices") or [])])
            if not _loc_ok(loc_re, loc):
                continue
            desc = BeautifulSoup(j.get("content", "") or "", "html.parser").get_text(" ")
            out.append({"id": f"gh_{slug}_{j.get('id')}", "title": j.get("title", ""),
                        "url": j.get("absolute_url", ""), "location": loc,
                        "description": desc[:_DESC_MAX], "ats": "greenhouse", "_wd": None,
                        "posted_at": norm_posted_date(j.get("first_published")
                                                      or j.get("updated_at"))})
    except Exception as e:
        print(f"    [!] greenhouse {slug}: {e}")
    return out


def fetch_lever_all(slug, loc_re=None):
    out = []
    try:
        data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                         f"lever {slug}")
        if not isinstance(data, list):
            return out
        for j in data:
            cats = j.get("categories", {}) or {}
            loc = _merge_locations(
                cats.get("location", ""),
                (cats.get("allLocations") or j.get("allLocations") or []))
            if not _loc_ok(loc_re, loc):
                continue
            job = {"id": f"lv_{slug}_{j.get('id')}", "title": j.get("text", ""),
                   "url": j.get("hostedUrl", ""), "location": loc,
                   "description": (j.get("descriptionPlain") or "")[:_DESC_MAX],
                   "ats": "lever", "_wd": None,
                   "posted_at": norm_posted_date(j.get("createdAt"))}
            if str(j.get("workplaceType", "")).lower() == "remote":
                job["remote_hint"] = "lever:workplaceType"
            out.append(job)
    except Exception as e:
        print(f"    [!] lever {slug}: {e}")
    return out


def fetch_ashby_all(slug, loc_re=None):
    out = []
    try:
        data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                         f"ashby {slug}")
        if not isinstance(data, dict):
            return out
        # Ashby's posting API returns {"jobs": [...], "apiVersion": "1"}.
        # `jobPostings` is the key in their GraphQL/embed payload, not this
        # one — reading it here made every Ashby board look empty.
        for j in data.get("jobs", data.get("jobPostings", [])):
            secondary = []
            for s in (j.get("secondaryLocations") or []):
                secondary.append(s.get("location", "") if isinstance(s, dict) else str(s))
            loc = _merge_locations(j.get("location", "") or "", secondary)
            if not _loc_ok(loc_re, loc):
                continue
            job = {"id": f"ashby_{slug}_{j.get('id')}", "title": j.get("title", ""),
                   "url": j.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{j.get('id')}",
                   "location": loc, "description": (j.get("descriptionPlain") or "")[:_DESC_MAX],
                   "ats": "ashby", "_wd": None,
                   "posted_at": norm_posted_date(j.get("publishedDate")
                                                 or j.get("publishedAt"))}
            if j.get("isRemote"):
                job["remote_hint"] = "ashby:isRemote"
            out.append(job)
    except Exception as e:
        print(f"    [!] ashby {slug}: {e}")
    return out


# Multi-location Workday rows list as literally "2 Locations" / "12 Locations"
# in locationsText — a string no locality regex can match, which silently
# dropped most multi-city reqs (NVIDIA Durham+Santa-Clara postings, BD
# "Firmware Engineer, Durham" + one more site). Those rows are rescued via
# the externalPath location slug and, failing that, the CXS detail's full
# location list (cached on disk — locations rarely change).
_WD_N_LOCATIONS_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)
_WD_LOC_CACHE_TTL = 3 * 24 * 3600


def _wd_cxs_headers():
    return {**HEADERS, "Accept": "application/json",
            "Content-Type": "application/json"}


# The /wday/cxs/{tenant}/ PATH segment is the internal tenant id, which for
# hyphenated subdomains is usually the UNDERSCORE form: vhr-unither.wd5's
# jobs endpoint is /wday/cxs/vhr_unither/External/jobs, and the hyphen form
# 422s. Observed live in the browser's own network traffic. Resolved once
# per tenant per process.
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
                timeout=20, headers=_wd_cxs_headers())
            if r.status_code == 200 and isinstance(r.json().get("total"), int):
                resolved = cand
                break
        except Exception:
            continue
    _WD_CXS_TENANT[key] = resolved
    return resolved


def _wd_path_location(path):
    """The human-readable location slug Workday embeds in externalPath:
    '/job/US-NC-Remote/Field-Application-Engineer_JR123' -> 'US NC Remote'."""
    m = re.match(r"/job/([^/]+)/", path or "")
    return m.group(1).replace("-", " ") if m else ""


def _wd_detail_locations(tenant, pod, site, path):
    """All locations of one Workday req from its CXS detail JSON (primary +
    additionalLocations), disk-cached. Returns a list of location strings
    ([] on any failure — callers must treat that as 'unknown', not 'no')."""
    cache = _BOARD_CACHE_DIR / "wdloc"
    key = hashlib.sha1(f"{tenant}|{path}".encode("utf-8")).hexdigest()
    p = cache / f"{key}.json"
    try:
        if time.time() - p.stat().st_mtime <= _WD_LOC_CACHE_TTL:
            return json.loads(p.read_text("utf-8")).get("locs", [])
    except Exception:
        pass
    # NOTE: externalPath already starts with "/job/"; the CXS detail URL is
    # {site}{path}, NOT {site}/job{path} (the doubled form 406s).
    api = (f"https://{tenant}.wd{pod}.myworkdayjobs.com"
           f"/wday/cxs/{_wd_cxs_tenant(tenant, pod, site)}/{site}{path}")
    locs = []
    try:
        r = SESSION.get(api, timeout=20,
                        headers={**HEADERS, "Accept": "application/json"})
        if r.status_code == 200:
            info = r.json().get("jobPostingInfo", {}) or {}
            locs = [info.get("location", "")] \
                + list(info.get("additionalLocations", []) or [])
            locs = [l for l in locs if l]
    except Exception:
        return []
    if locs:  # cache only decided outcomes, like the board cache
        try:
            cache.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"locs": locs}), encoding="utf-8")
        except Exception:
            pass
    return locs


def _wd_location_facets(api, hdr, loc_re):
    """Server-side location filter: read page-0 facets, collect the ids of
    every location-flavored value whose descriptor matches loc_re, grouped
    by the facetParameter that owns it ('locations' on most tenants).
    Returns an appliedFacets dict, or {} when the tenant exposes none —
    callers fall back to free-text search. Solves tenants like Labcorp
    whose searchText ignores state names ('North Carolina' -> 2 hits vs
    191 real NC reqs across 46 city facet values)."""
    try:
        r = SESSION.post(api, json={"appliedFacets": {}, "limit": 1,
                                    "offset": 0, "searchText": ""},
                         timeout=25, headers=hdr)
        facets = r.json().get("facets", []) or []
    except Exception:
        return {}

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
    return applied


def _default_search_text():
    """A free-text location term for Workday's `searchText`, from [locality].

    Prefer a spelled-out state/region suffix ("california") over a two-letter
    abbreviation, which matches far too much in a free-text field; fall back
    to the longest place name. Returns "" when no locality is configured,
    which simply means an unnarrowed board pull."""
    words = [s for s in config.LOCALITY_STATE_SUFFIX if len(s) > 2]
    if words:
        return max(words, key=len)
    places = [s for s in config.LOCALITY_SUBSTRINGS if s]
    return max(places, key=len) if places else ""


def fetch_workday_all(tenant, pod, site, loc_re=None, search_text=None,
                      page_size=20, max_pages=60):
    """List postings (title/location/path only). Descriptions hydrated later.

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

    `search_text` defaults to a term derived from your [locality]; pass "" for
    an explicitly unnarrowed pull.
    """
    host = f"https://{tenant}.wd{pod}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{_wd_cxs_tenant(tenant, pod, site)}/{site}/jobs"
    link = f"{host}/en-US/{site}"
    hdr = _wd_cxs_headers()

    applied_facets = {}
    if loc_re is not None:
        applied_facets = _wd_location_facets(api, hdr, loc_re)
    # Never send a location term on a whole-board pull: loc_re=None IS the
    # request for everything, and narrowing it anyway silently hid every
    # out-of-area posting from callers that asked for the full board.
    if search_text is None:
        search_text = _default_search_text() if loc_re is not None else ""
    body_extra = ({"appliedFacets": applied_facets, "searchText": ""}
                  if applied_facets else
                  {"appliedFacets": {}, "searchText": search_text})

    out = []
    for page in range(max_pages):
        try:
            r = SESSION.post(api, json={**body_extra, "limit": page_size,
                                        "offset": page * page_size},
                             timeout=25, headers=hdr)
            posts = r.json().get("jobPostings", []) or []
        except Exception as e:
            print(f"    [!] workday {tenant} p{page}: {e}")
            break
        if not posts:
            break
        for p in posts:
            loc = p.get("locationsText", "") or ""
            path = p.get("externalPath", "") or ""
            if not _loc_ok(loc_re, loc):
                # Rescue 1: the externalPath's location slug (free).
                slug_loc = _wd_path_location(path)
                if _loc_ok(loc_re, slug_loc):
                    loc = f"{slug_loc}" + (f" ({loc})" if loc else "")
                # Rescue 2: "N Locations" rows — full list via CXS detail.
                elif _WD_N_LOCATIONS_RE.match(loc) and path:
                    locs = _wd_detail_locations(tenant, pod, site, path)
                    if not any(_loc_ok(loc_re, l) for l in locs):
                        continue
                    loc = "; ".join(locs)
                else:
                    continue
            elif _WD_N_LOCATIONS_RE.match(loc) and path:
                # Facet-filtered fetch already vouches this req is in-area,
                # but "2 Locations" is useless downstream (geo_mode, ranking
                # location filters) — resolve the real list.
                locs = _wd_detail_locations(tenant, pod, site, path)
                if locs:
                    loc = "; ".join(locs)
            jid = path.rsplit("/", 1)[-1] if path else str(abs(hash(p.get("title", "") + loc)))
            out.append({"id": f"wd_{tenant}_{jid}", "title": p.get("title", ""),
                        "url": f"{link}{path}" if path else host, "location": loc,
                        "description": "", "ats": "workday",
                        "_wd": (tenant, pod, site, path),
                        # relative text ("Posted 30+ Days Ago") — approximate
                        "posted_at": norm_posted_date(p.get("postedOnDate")
                                                      or p.get("postedOn"))})
        if len(posts) < page_size:
            break
    return out


def fetch_smartrecruiters_all(slug, loc_re=None, max_pages=10):
    """SmartRecruiters public postings API. Descriptions hydrated lazily."""
    out = []
    for page in range(max_pages):
        try:
            r = SESSION.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
                             f"?limit=100&offset={page*100}", timeout=25, headers=HEADERS)
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
            if not _loc_ok(loc_re, loc_s):
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


# iCIMS's WAF 405s Chrome-like UAs that arrive without Chrome's client-hint
# headers (sec-ch-ua etc.) — i.e. exactly what a requests session claiming
# Chrome looks like. A plain Mozilla platform UA passes. Module-level so the
# hydration path can reuse it.
_ICIMS_HEADERS = {**HEADERS,
                  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_ICIMS_LOC_CACHE_TTL = 7 * 24 * 3600
_ICIMS_META_CAP = 150            # detail GETs per fetch call, tops


def _icims_job_meta(url, need_desc=False):
    """(location, description) for one iCIMS posting from its own detail
    page. `?in_iframe=1` serves the full server-rendered document — JSON-LD
    included — even on tenants whose search UI is a JS shell (careers-sas).
    Location is disk-cached (postings rarely move); description is not, so
    pass need_desc=True to force a fetch past the location cache.
    Returns ('', '') on a miss."""
    m = re.search(r"/jobs/(\d+)/", url or "")
    if not m:
        return "", ""
    cache = _BOARD_CACHE_DIR / "icimsloc"
    host = re.sub(r"^https?://", "", url).split("/")[0]
    p = cache / f"{host}_{m.group(1)}.json"
    cached_loc = None
    try:
        if time.time() - p.stat().st_mtime <= _ICIMS_LOC_CACHE_TTL:
            cached_loc = json.loads(p.read_text("utf-8")).get("loc", "")
    except Exception:
        pass
    sep = "&" if "?" in url else "?"
    detail_url = url if "in_iframe" in url else f"{url}{sep}in_iframe=1"
    if cached_loc is not None and not need_desc:
        return cached_loc, ""    # description fetched on demand by hydration
    from .jsonld import _normalize_description, _normalize_location, extract_jsonld, is_jobposting
    loc = desc = ""
    try:
        r = SESSION.get(detail_url, timeout=20, headers=_ICIMS_HEADERS)
        if r.status_code == 200:
            for obj in extract_jsonld(r.text):
                if is_jobposting(obj):
                    loc = _normalize_location(obj)
                    loc = "" if loc == "Unknown" else loc
                    desc = _normalize_description(obj)
                    break
    except Exception:
        return "", ""
    if loc:
        try:
            cache.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"loc": loc}), encoding="utf-8")
        except Exception:
            pass
    return loc, desc


def fetch_icims_all(tenant, loc_re=None, loc_label="NC", search_location="NC"):
    """
    Best-effort iCIMS scrape via the public job-search page. iCIMS is often
    JS-gated; this catches the server-rendered rows and returns [] otherwise.

    searchLocation is a free-text query some tenants don't parse ("NC" gets
    'No Results Found' on careers-sas / incareers-acentra) — so a located
    search that comes back empty is retried WITHOUT the location param.
    Paginated: server-rendered tenants expose ?pr=N result pages.

    Locations are REAL, not assumed: rows first try a "City, ST" read from
    their own search-row text; anything still unlocated is resolved from the
    posting's detail page (JSON-LD via ?in_iframe=1, disk-cached) before
    loc_re filtering. Only a located-search row that resists both keeps the
    loc_label approximation — the server already filtered those to the area.
    (The old code stamped EVERY row loc_label, so a global tenant's Austin
    and remote postings all entered the store labeled "NC".)
    """
    out = []

    def _one_page(params, located):
        found = []
        r = SESSION.get(f"https://{tenant}.icims.com/jobs/search?ss=1&in_iframe=1"
                        + params, timeout=20, headers=_ICIMS_HEADERS)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a.iCIMS_Anchor, a[href*='/jobs/']"):
            # Row anchors carry a screen-reader label ("Requisition Title")
            # ahead of the actual title text.
            title = re.sub(r"^\s*Requisition Title\s*", "",
                           a.get_text(" ").strip())
            href = a.get("href", "")
            jid = re.search(r"/jobs/(\d+)/", href)
            # A numbered detail URL is what distinguishes a posting row from
            # the search shell's own chrome (/jobs/intro, /jobs/search links)
            # — junk rows here would mask the sitemap fallback below.
            if not title or not jid:
                continue
            row = a.find_parent()
            row_text = row.get_text(" ") if row else ""
            lm = _LOC_TEXT_RE.search(row_text)
            loc = (lm.group(0).strip() if lm else (loc_label if located else ""))
            found.append({"id": f"icims_{tenant}_{jid.group(1)}",
                          "title": title, "url": href if href.startswith("http")
                          else f"https://{tenant}.icims.com{href}", "location": loc,
                          "description": "", "ats": "icims", "_wd": None,
                          "_row_text": row_text})
        return found

    try:
        out = _one_page(f"&searchLocation={search_location}", located=True)
        if not out:
            for page in range(1, 9):   # locationless, paged
                batch = _one_page(f"&pr={page - 1}" if page > 1 else "", located=False)
                if not batch:
                    break
                out.extend(batch)
        if not out:
            # Fully JS-rendered tenant (careers-sas): the search page serves a
            # shell, but /sitemap.xml lists every live posting. Titles come
            # from the URL slug; locations resolved per job below.
            from urllib.parse import unquote
            r = SESSION.get(f"https://{tenant}.icims.com/sitemap.xml",
                            timeout=20, headers=_ICIMS_HEADERS)
            for u in re.findall(r"<loc>([^<]+)</loc>", r.text):
                m = re.search(r"/jobs/(\d+)/([^/]+)/job", u)
                if not m:
                    continue
                title = unquote(m.group(2)).replace("---", " - ").replace("-", " ").strip()
                out.append({"id": f"icims_{tenant}_{m.group(1)}", "title": title,
                            "url": u, "location": "",
                            "description": "", "ats": "icims", "_wd": None})
        # De-dup across pages (last page repeats on some tenants).
        seen, uniq = set(), []
        for j in out:
            if j["id"] not in seen:
                seen.add(j["id"])
                uniq.append(j)
        out = uniq
        # Resolve unlocated rows from their own detail pages (cached), then
        # apply the location filter against what the posting really says.
        n_meta = 0
        for j in out:
            if not j["location"] and n_meta < _ICIMS_META_CAP:
                loc, desc = _icims_job_meta(j["url"])
                n_meta += 1
                if loc:
                    j["location"] = loc
                if desc and not j["description"]:
                    j["description"] = desc[:_DESC_MAX]
            j.pop("_row_text", None)
        if loc_re is not None:
            out = [j for j in out
                   if _loc_ok(loc_re, j["location"])
                   or (not j["location"] and _loc_ok(loc_re, j["title"]))]
    except Exception as e:
        print(f"    [!] icims {tenant}: {e}")
    return out


def fetch_jazzhr_all(slug, loc_re=None, max_jobs=60, per_job_delay=0.3):
    """Full JazzHR board, ungated. Mirrors fetchers.jazzhr.fetch_jazzhr but
    skips its is_relevant() filter — the caller's own filter chain decides,
    same contract as the other *_all fetchers in this module. Needed because
    fetch_jazzhr's per-job JSON-LD parse (fetchers/jsonld.py) drops any
    posting that doesn't match whatever keyword tier is live in config at
    call time, which silently starves company-vetted callers (board hydrate,
    the local-tech NC crawl) of postings with generic engineering titles."""
    import re
    import time as _time

    from .jsonld import _job_from_posting, extract_jsonld, is_jobposting

    base = f"https://{slug}.applytojob.com"
    try:
        r = SESSION.get(base + "/", timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] JazzHR {slug}: {e}")
        return []

    apply_re = re.compile(r"/apply/[A-Za-z0-9]+/[A-Za-z0-9_-]+")
    seen, urls = set(), []
    for path in apply_re.findall(r.text):
        url = base + path
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= max_jobs:
            break

    out = []
    for url in urls:
        try:
            jr = SESSION.get(url, timeout=20, headers=HEADERS)
            jr.raise_for_status()
        except Exception as e:
            print(f"    [!] JazzHR {slug} {url}: {e}")
            continue
        for obj in extract_jsonld(jr.text):
            if not is_jobposting(obj):
                continue
            job = _job_from_posting(obj, slug, url)
            if not _loc_ok(loc_re, job.get("location", "")):
                continue
            out.append({"id": job["id"], "title": job.get("title", ""),
                        "url": job.get("url", ""), "location": job.get("location", ""),
                        "description": job.get("description", ""),
                        "ats": "jazzhr", "_wd": None})
        _time.sleep(per_job_delay)
    return out


def fetch_bamboohr_all(slug, loc_re=None, max_details=200, detail_delay=0.15):
    """Full BambooHR board, ungated (mirrors fetchers.bamboohr.fetch_bamboohr
    but skips its is_relevant() title/dept pre-filter)."""
    import time as _time

    from .bamboohr import _fetch_description, _is_remote, _location_str

    base = f"https://{slug}.bamboohr.com"
    try:
        r = SESSION.get(f"{base}/careers/list", timeout=20,
                         headers={**HEADERS, "Accept": "application/json"})
        r.raise_for_status()
        entries = r.json().get("result") or []
    except Exception as e:
        print(f"    [!] BambooHR {slug}: {e}")
        return []

    out = []
    for i, entry in enumerate(entries):
        jid = str(entry.get("id") or "")
        title = entry.get("jobOpeningName") or ""
        if not jid or not title:
            continue
        loc = _location_str(entry)
        if not _loc_ok(loc_re, loc):
            continue
        desc = _fetch_description(base, jid) if i < max_details else ""
        if i < max_details:
            _time.sleep(detail_delay)
        job = {"id": f"bamboo_{slug}_{jid}", "title": title,
               "url": f"{base}/careers/{jid}", "location": loc,
               "description": desc, "ats": "bamboohr", "_wd": None}
        if _is_remote(entry):
            job["remote_hint"] = "bamboohr:locationType"
        out.append(job)
    return out


def fetch_adp_all(slug, loc_re=None, page_size=50, max_pages=10,
                  max_details=200, detail_delay=0.15):
    """Full ADP Workforce Now board, ungated (mirrors fetchers.adp_wfn but
    skips its is_relevant() title pre-filter). `slug` is "cid|ccid"."""
    import time as _time

    from .adp_wfn import _API, _PORTAL, _fetch_description, _location_str

    cid, ccid = slug.split("|", 1)
    out, details_fetched = [], 0
    for page in range(max_pages):
        try:
            r = SESSION.get(
                _API, params={"cid": cid, "ccId": ccid, "locale": "en_US",
                              "$top": page_size, "$skip": page * page_size},
                timeout=25, headers={**HEADERS, "Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"    [!] ADP {cid[:8]} p{page}: {e}")
            break
        reqs = data.get("jobRequisitions") or []
        if not reqs:
            break
        for req in reqs:
            item_id = str(req.get("itemID") or "")
            title = req.get("requisitionTitle") or ""
            if not item_id or not title:
                continue
            loc = _location_str(req)
            if not _loc_ok(loc_re, loc):
                continue
            desc = ""
            if details_fetched < max_details:
                desc = _fetch_description(item_id, cid, ccid)
                details_fetched += 1
                _time.sleep(detail_delay)
            out.append({"id": f"adp_{cid[:8]}_{item_id}", "title": title,
                        "url": f"{_PORTAL}?cid={cid}&ccId={ccid}&jobId={item_id}&lang=en_US",
                        "location": loc, "description": desc,
                        "ats": "adp", "_wd": None})
        if len(reqs) < page_size:
            break
        _time.sleep(0.3)
    return out


def fetch_paylocity_all(guid, loc_re=None, max_details=200, detail_delay=0.15):
    """Full Paylocity board, ungated (mirrors fetchers.paylocity.fetch_paylocity
    without its is_relevant() gate). Location-filters from the embedded
    listing before paying for any detail-page description fetch."""
    import time as _time

    from .paylocity import _DETAIL, fetch_description, location_str, parse_board

    try:
        raw = parse_board(guid)
    except Exception as e:
        print(f"    [!] Paylocity {guid[:8]}: {e}")
        return []
    out, fetched = [], 0
    for j in raw:
        jid = str(j.get("JobId") or "")
        title = j.get("JobTitle") or ""
        if not jid or not title:
            continue
        loc = location_str(j)
        if not _loc_ok(loc_re, loc):
            continue
        desc = ""
        if fetched < max_details:
            desc = fetch_description(jid)
            fetched += 1
            _time.sleep(detail_delay)
        job = {"id": f"paylocity_{guid[:8]}_{jid}", "title": title,
               "url": _DETAIL.format(jid=jid), "location": loc,
               "description": desc, "ats": "paylocity", "_wd": None}
        if j.get("IsRemote"):
            job["remote_hint"] = "paylocity:isRemote"
        out.append(job)
    return out


def fetch_rippling_all(slug, loc_re=None, max_details=200, detail_delay=0.15):
    """Full Rippling board, ungated (mirrors fetchers.rippling.fetch_rippling
    without its is_relevant() gate). Location-filters from the listing before
    paying for a detail-page description fetch."""
    import time as _time

    from .rippling import fetch_description, location_str, parse_board

    try:
        raw = parse_board(slug)
    except Exception as e:
        print(f"    [!] Rippling {slug}: {e}")
        return []
    out, fetched = [], 0
    for j in raw:
        uuid = j.get("uuid") or ""
        title = (j.get("name") or "").strip()
        if not uuid or not title:
            continue
        loc = location_str(j)
        if not _loc_ok(loc_re, loc):
            continue
        desc = ""
        if fetched < max_details:
            desc = fetch_description(slug, uuid)
            fetched += 1
            _time.sleep(detail_delay)
        out.append({"id": f"rippling_{slug}_{uuid[:12]}", "title": title,
                    "url": j.get("url") or f"https://ats.rippling.com/{slug}/jobs/{uuid}",
                    "location": loc, "description": desc, "ats": "rippling", "_wd": None})
    return out


def fetch_hibob_all(tenant, loc_re=None):
    """Full HiBob board, ungated (mirrors fetchers.hibob.fetch_hibob without
    its is_relevant() gate). The description is inline in the listing, so —
    like UltiPro — there's no separate detail call to skip."""
    from bs4 import BeautifulSoup

    from .hibob import location_str, parse_board

    try:
        raw = parse_board(tenant)
    except Exception as e:
        print(f"    [!] HiBob {tenant}: {e}")
        return []
    out = []
    for j in raw:
        jid = str(j.get("id") or "")
        title = (j.get("title") or "").strip()
        if not jid or not title:
            continue
        loc = location_str(j)
        if not _loc_ok(loc_re, loc):
            continue
        desc = BeautifulSoup(j.get("description") or "", "html.parser").get_text(" ", strip=True)
        job = {"id": f"hibob_{tenant}_{jid[:12]}", "title": title,
               "url": f"https://{tenant}.careers.hibob.com/jobs",
               "location": loc, "description": desc, "ats": "hibob", "_wd": None}
        if str(j.get("workspaceType", "")).lower() == "remote":
            job["remote_hint"] = "hibob:workspaceType"
        out.append(job)
    return out


def fetch_ultipro_all(slug, loc_re=None):
    """Full UKG Pro (UltiPro) board, ungated (mirrors fetchers.ultipro.fetch_ultipro
    without its is_relevant() gate). BriefDescription is inline, so no detail call."""
    from .ultipro import _desc, _detail_url, location_str, parse_board
    try:
        opps = parse_board(slug)
    except Exception as e:
        print(f"    [!] UltiPro {slug.split('|')[0]}: {e}")
        return []
    code = slug.split("|")[0]
    out = []
    for o in opps:
        title = (o.get("Title") or "").strip()
        oid = o.get("Id") or ""
        if not title or not oid:
            continue
        loc = location_str(o)
        if not _loc_ok(loc_re, loc):
            continue
        out.append({"id": f"ultipro_{code}_{oid[:12]}", "title": title,
                    "url": _detail_url(slug, oid), "location": loc,
                    "description": _desc(o), "ats": "ultipro", "_wd": None})
    return out


def fetch_kula_all(slug, loc_re=None):
    """Full Kula board, ungated (mirrors fetchers.html_scrape.fetch_kula but
    skips its is_relevant() pre-filter). Kula never exposes a listing-page
    description; callers needing the body must hit the per-job URL themselves."""
    import re

    from urllib.parse import urljoin

    base_url = f"https://careers.kula.ai/{slug}"
    try:
        r = SESSION.get(base_url, timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Kula {slug}: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    apply_links = soup.find_all("a", href=re.compile(rf"/{re.escape(slug)}/\d+"))
    out = []
    for a in apply_links:
        href = a["href"]
        if not href.startswith("http"):
            href = urljoin("https://careers.kula.ai", href)
        jid = re.search(r"/(\d+)/?$", href)
        if not jid:
            continue
        parent = a.parent
        lines = []
        for _ in range(8):
            raw = parent.get_text("\n").strip()
            lines = [l.strip() for l in raw.split("\n") if len(l.strip()) > 3]
            if len(lines) >= 2:
                break
            parent = parent.parent
        title = lines[1] if len(lines) > 1 else lines[0] if lines else "Unknown"
        loc = lines[2] if len(lines) > 2 else "See posting"
        loc = loc.split(";")[0].strip()
        if not _loc_ok(loc_re, loc):
            continue
        out.append({"id": f"kula_{slug}_{jid.group(1)}", "title": title,
                    "url": href, "location": loc, "description": "",
                    "ats": "kula", "_wd": None})
    return out


def hydrate_description(job):
    """Fetch a job's real description (in place) for ATSes with a detail call."""
    if job.get("description"):
        return job
    if job.get("ats") == "workday" and job.get("_wd"):
        tenant, pod, site, path = job["_wd"]
        # externalPath already begins "/job/..." — appending it to ".../job"
        # doubled the segment and 406'd, silently pushing every Workday row
        # onto the slower public-page JSON-LD fallback below.
        api = (f"https://{tenant}.wd{pod}.myworkdayjobs.com"
               f"/wday/cxs/{_wd_cxs_tenant(tenant, pod, site)}/{site}{path}")
        try:
            r = SESSION.get(api, timeout=20, headers={**HEADERS, "Accept": "application/json"})
            info = r.json().get("jobPostingInfo", {}) or {}
            html = info.get("jobDescription", "") or ""
            job["description"] = BeautifulSoup(html, "html.parser").get_text(" ")[:_DESC_MAX]
            # Same JSON carries the req's full location list — upgrade a
            # useless "2 Locations" locationsText to the real thing.
            locs = [info.get("location", "")] \
                + list(info.get("additionalLocations", []) or [])
            locs = [l for l in locs if l]
            if locs and (not job.get("location")
                         or _WD_N_LOCATIONS_RE.match(job["location"] or "")):
                job["location"] = "; ".join(locs)
        except Exception:
            pass
    elif job.get("ats") == "smartrecruiters" and job.get("_sr"):
        slug, pid = job["_sr"]
        try:
            r = SESSION.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}",
                             timeout=20, headers=HEADERS)
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
        loc, desc = _icims_job_meta(job["url"], need_desc=True)
        if desc:
            job["description"] = desc[:_DESC_MAX]
        if loc and (job.get("location") or "").strip() in ("", "NC"):
            job["location"] = loc
    elif job.get("ats") == "wpjson" and job.get("url"):
        # Outbound apply page (restor3d: Arcoro/BirdDog portal). Server-
        # rendered; the JD sits in #portalViewRequirement. The generic
        # fallback below still runs on a miss (e.g. a WP permalink URL).
        try:
            html = SESSION.get(job["url"], timeout=25, headers=HEADERS).text
            soup = BeautifulSoup(html, "lxml")
            el = (soup.select_one("#portalViewRequirement")
                  or soup.select_one('[class*="bmportalrequirementdetails"]'))
            if el:
                job["description"] = el.get_text(" ", strip=True)[:_DESC_MAX]
        except Exception:
            pass
    # Generic fallback: any job with a detail URL whose ATS-specific branch
    # didn't yield a body (SuccessFactors career sites like Duke/Teleflex whose
    # slug is unknown, custom boards, Workday rows that arrived without _wd).
    # Covers the empty-description rows that were silently unscorable.
    if not job.get("description") and job.get("url"):
        d = _description_from_job_url(job["url"])
        if d:
            job["description"] = d
    return job


def _description_from_job_url(url):
    """Best-effort JD text from a job's own detail page, vendor-agnostically:
    schema.org JSON-LD JobPosting first (hundreds of sites), then SuccessFactors
    Career-Site-Builder markup (data-careersite-propertyid='description' — Duke,
    Teleflex, and other SAP SF frontends). Returns '' on miss."""
    try:
        r = SESSION.get(url, timeout=20, headers=HEADERS,
                        allow_redirects=True)
        if r.status_code in (403, 405):
            # WAFs (iCIMS) that reject a Chrome UA without Chrome's
            # client-hint headers accept a plain platform UA — same quirk
            # fetch_icims_all works around.
            r = SESSION.get(url, timeout=20, allow_redirects=True,
                            headers={**HEADERS, "User-Agent":
                                     "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        html = r.text
    except Exception:
        return ""
    try:
        from .jsonld import extract_jsonld, is_jobposting, _normalize_description
        for obj in extract_jsonld(html):
            if is_jobposting(obj):
                d = _normalize_description(obj).strip()
                if len(d) >= 120:
                    return d[:_DESC_MAX]
    except Exception:
        pass
    try:
        soup = BeautifulSoup(html, "lxml")
        el = (soup.select_one('[data-careersite-propertyid="description"]')
              or soup.select_one('[data-careersite-propertyid="jobdescription"]')
              # Custom boards that name the JD container (science.xyz: "_flow
              # job-description"). Kept specific — 'job-description'/'jobDescription',
              # not a bare 'description' — so a short company tagline can't match.
              or soup.select_one('[class*="job-description"]')
              or soup.select_one('[class*="jobDescription"]')
              # SuccessFactors' CLASSIC (pre-Career-Site-Builder) template
              # wraps the posting in .jobDisplay (Teleflex). Last in the chain:
              # it carries a little page chrome, so the precise containers win.
              or soup.select_one('[class*="jobDisplay"]'))
        if el:
            d = el.get_text(" ", strip=True)
            if len(d) >= 120:
                return d[:_DESC_MAX]
    except Exception:
        pass
    return ""


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
    no recognizable signal) — callers must leave stored status alone on None.
    Only used for rows the crawl's board-diff can't cover (see
    tracks.local_tech.check_closed_jobs); board snapshots are authoritative
    where available.
    """
    if not url:
        return None, "no url"
    if _GATED_HOST_RE.search(url):
        return None, "bot-gated aggregator host"

    # Workday: the CXS JSON detail endpoint is authoritative and JS-free.
    # Hyphenated tenants need the underscore tenant id in the CXS path
    # (vhr_unither), so each variant is tried before concluding anything.
    from .workday import _cxs_detail_url, _cxs_tenant_variants
    cxs = _cxs_detail_url(url)
    if cxs:
        last_status = None
        for u in _cxs_tenant_variants(cxs):
            try:
                r = SESSION.get(u, timeout=20,
                                headers={**HEADERS, "Accept": "application/json"})
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
        r = SESSION.get(url, timeout=20, headers=HEADERS, allow_redirects=True)
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


def hydrate_from_company_board(job, company):
    """Backfill an empty description by title-matching this job against its
    company's own ATS board (a full, ungated FETCHERS pull).

    For jobs that arrived with only a title — manually captured LinkedIn
    search-card rows (scrapers/page_capture.py), or any other curated
    ingest — `description` is never populated, so they permanently fail
    fit.py's MIN_DESC_CHARS gate. When the job is linked to a company with a
    resolvable board, we can recover the real description by pulling that
    board and matching on title. No-op if the job already has a description
    or the company has no fetchable board.
    """
    if (job.get("description") or "").strip() or not company or not company.get("ats"):
        return job
    title_l = (job.get("title") or "").strip().lower()
    if not title_l:
        return job
    try:
        board = fetch_company(company, loc_re=None)
    except Exception as e:
        print(f"    [!] board-hydrate fetch failed for {company.get('name')}: {e}")
        return job
    match = next((b for b in board if (b.get("title") or "").strip().lower() == title_l), None)
    if match is None:
        return job
    hydrate_description(match)
    if match.get("description"):
        job["description"] = match["description"]
        job["url"] = job.get("url") or match.get("url")
    return job


def _adapt(jobs, ats, loc_re):
    """Normalize an existing fetcher's dicts to the company-fetch shape."""
    out = []
    for j in jobs:
        if not _loc_ok(loc_re, j.get("location", "")):
            continue
        out.append({"id": j["id"], "title": j.get("title", ""), "url": j.get("url", ""),
                    "location": j.get("location", ""), "description": j.get("description", ""),
                    "ats": ats, "_wd": None})
    return out


def fetch_successfactors_all(base_url, loc_re=None, step=25, max_pages=80):
    """Full SuccessFactors board, ungated. Mirrors html_scrape.fetch_successfactors
    but (a) drops its is_relevant() keyword gate — company-vetted callers pull
    the whole board and their own filter chain decides; multi-division orgs
    (Duke) are re-gated in the local track's _keep_job — and (b) recovers the
    location from the SF job-detail slug (``/job/Durham,-NC-<title>-NC-27701/``)
    when the listing row hides it, so loc_re filtering works on boards like
    OXB's where the row carries no location text."""
    from urllib.parse import unquote, urljoin

    out, seen = [], set()
    sf_headers = {**HEADERS, "Accept": "text/html"}
    # SF slug head is "<City>,-<ST>-<Title>..." with spaces encoded as hyphens
    # (e.g. "/job/Holly-Springs,-NC-<title>-NC-27540/"). Non-greedy up to the
    # first ",-<2 caps>-" so a comma inside the title can't steal the match.
    loc_slug_re = re.compile(r"/job/(.+?),-([A-Z]{2})-")
    for page in range(max_pages):
        url = f"{base_url.rstrip('/')}/search/?startrow={page * step}"
        try:
            r = SESSION.get(url, timeout=25, headers=sf_headers)
            r.raise_for_status()
        except Exception as e:
            print(f"    [!] SuccessFactors {base_url} p{page}: {e}")
            break
        soup = BeautifulSoup(r.text, "html.parser")
        anchors = soup.select("a.jobTitle-link") or [
            a for a in soup.find_all("a", href=True) if "/job/" in a["href"]]
        if not anchors:
            break
        new_on_page = 0
        for a in anchors:
            href = a.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = urljoin(base_url, href)
            if href in seen:
                continue
            seen.add(href)
            new_on_page += 1
            # Location: SF encodes "City,-ST" at the head of the /job/ slug.
            # Fall back to the surrounding row text, then "See posting".
            loc = ""
            m = loc_slug_re.search(unquote(href))
            if m:
                loc = f"{m.group(1).replace('-', ' ').strip()}, {m.group(2)}"
            else:
                row = a.find_parent("tr") or a.find_parent("li") or a.find_parent("div")
                loc = (location_snippet(row.get_text(" ", strip=True))
                       if row is not None else "See posting")
            if not _loc_ok(loc_re, loc):
                continue
            jid_m = re.search(r"/job/[^/]+/(\d+)", href) or re.search(r"/job/([^/?#]+)", href)
            jid = jid_m.group(1) if jid_m else str(abs(hash(href)))
            out.append({"id": f"sf_{re.sub(r'[^a-z0-9]+','',base_url.lower())[-16:]}_{jid}",
                        "title": a.get_text(strip=True), "url": href,
                        "location": loc, "description": "", "ats": "successfactors",
                        "_wd": None})
        if new_on_page == 0:
            break
    return out


def fetch_peopleadmin_all(host, loc_re=None):
    from . import fetch_peopleadmin
    return _adapt(fetch_peopleadmin(host, ""), "peopleadmin", loc_re)


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
# City, ST  |  City, State  |  Remote
_LOC_TEXT_RE = re.compile(r"[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+)*,\s*"
                          r"(?:[A-Z]{2}|[A-Z][a-z]+)\b|\bremote\b", re.I)
_OPENINGS_HREF_RE = re.compile(
    r"/(open-positions|open-roles|career-opportunities|current-openings|"
    r"job-openings|openings|opportunities|positions|jobs)\b", re.I)
# scheme+host extractor and the openings link-text cue — precompiled once
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
        # Prefer a heading/title element for a clean title (Science nests the
        # title + location in one <a>); fall back to the full link text.
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
    Won't follow off to Indeed/LinkedIn/an ATS — those aren't a custom board."""
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
            continue  # off-domain — skip
        if _OFFSITE_RE.search(absu):
            continue
        text = a.get_text(" ", strip=True).lower()
        if _OPENINGS_HREF_RE.search(href) or _OPENINGS_TEXT_RE.search(text):
            return absu
    return None


def _location_near(a, loc_re=None):
    """
    Best-effort location for a job link: search the link then its container.
    Prefers a loc_re (e.g. NC) location when the container is multi-location,
    so a role listed "Alameda, CA | Durham, NC" is kept as a Durham job.
    """
    for el in (a, a.parent, a.parent.parent if a.parent else None):
        if el is None:
            continue
        text = el.get_text(" ", strip=True)
        if loc_re is not None:
            m = loc_re.search(text)
            if m:
                return m.group(0)
        m = _LOC_TEXT_RE.search(text)
        if m:
            return m.group(0)
    return ""


def _get_soup(url):
    try:
        r = SESSION.get(url, timeout=20, headers=HEADERS)
        if r.status_code != 200:
            return None
        return BeautifulSoup(r.text, "lxml")
    except Exception:
        return None


def _get_anchor_soup(url):
    """Like _get_soup but parses only <a> tags — for callers that just count
    or scan job/openings links (no surrounding-container reads)."""
    try:
        r = SESSION.get(url, timeout=20, headers=HEADERS)
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
    postings. Covers boards like Science Corp (science.xyz).
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
        if not _loc_ok(loc_re, loc):
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
# TTL is deliberately SHORT so a board that later goes live — or one that goes
# dead — is re-checked within the window rather than pinned by a stale negative.
# Transient fetch failures are NOT cached (only decided outcomes), so a network
# blip never suppresses a real board.
_BOARD_CACHE_DIR = config.DATA_DIR / ".cache" / "board"
_BOARD_CACHE_TTL = 6 * 3600      # seconds


def _board_cache_get(url):
    """(listing_or_None,) on a live entry, or None on miss/expired/error.
    The 1-tuple lets callers distinguish a cached negative from a miss."""
    p = _BOARD_CACHE_DIR / f"{hashlib.sha1(url.encode('utf-8')).hexdigest()}.json"
    try:
        if time.time() - p.stat().st_mtime > _BOARD_CACHE_TTL:
            return None
        return (json.loads(p.read_text("utf-8")).get("listing"),)
    except Exception:
        return None


def _board_cache_put(url, listing):
    try:
        _BOARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _BOARD_CACHE_DIR / f"{hashlib.sha1(url.encode('utf-8')).hexdigest()}.json"
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
        return None  # transient fetch failure — do NOT cache
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
    """WordPress "post-filters-archive" careers endpoint (restor3d-class
    themes): {root}/wp-json/post-filters-archive/get-posts?post_type=career.

    For sites whose careers grid AND per-job pages are all JS-rendered, so
    neither fetch_custom_careers (no server-side anchors) nor the JSON-LD
    path sees anything — but the theme's own REST route serves clean JSON.
    The stored URL is the posting's outbound apply link (restor3d: an
    Arcoro/BirdDog portal page, server-rendered); hydrate_description's
    "wpjson" branch pulls the JD text from it."""
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
            if not _loc_ok(loc_re, loc):
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


FETCHERS = {
    "greenhouse":      lambda c, lr: fetch_greenhouse_all(c["slug"], lr),
    "lever":           lambda c, lr: fetch_lever_all(c["slug"], lr),
    "ashby":           lambda c, lr: fetch_ashby_all(c["slug"], lr),
    "jazzhr":          lambda c, lr: fetch_jazzhr_all(c["slug"], lr),
    "bamboohr":        lambda c, lr: fetch_bamboohr_all(c["slug"], lr),
    "adp":             lambda c, lr: fetch_adp_all(c["slug"], lr),
    "kula":            lambda c, lr: fetch_kula_all(c["slug"], lr),
    "paylocity":       lambda c, lr: fetch_paylocity_all(c["slug"], lr),
    "rippling":        lambda c, lr: fetch_rippling_all(c["slug"], lr),
    "ultipro":         lambda c, lr: fetch_ultipro_all(c["slug"], lr),
    "hibob":           lambda c, lr: fetch_hibob_all(c["slug"], lr),
    "workday":         lambda c, lr: fetch_workday_all(c["wd_tenant"], c["wd_pod"], c["wd_site"], lr),
    "smartrecruiters": lambda c, lr: fetch_smartrecruiters_all(c["slug"], lr),
    "icims":           lambda c, lr: fetch_icims_all(c["slug"], lr),
    "successfactors":  lambda c, lr: fetch_successfactors_all(c["careers_url"], lr),
    "peopleadmin":     lambda c, lr: fetch_peopleadmin_all(c["careers_url"], lr),
    "custom":          lambda c, lr: fetch_custom_careers(c["careers_url"], lr),
    "wpjson":          lambda c, lr: fetch_wpjson_careers_all(c["careers_url"], lr),
}


def fetch_company(company, loc_re=None):
    """Dispatch to the right fetcher for a company dict from the store.

    `loc_re=None` pulls the whole board; pass NC_RE for a Triangle-scoped
    pull (the local track's default).
    """
    fn = FETCHERS.get(company.get("ats"))
    return fn(company, loc_re) if fn else []


# Back-compat alias for callers written against the old local_fetch module.
def fetch_company_nc(company):
    return fetch_company(company, NC_RE)
