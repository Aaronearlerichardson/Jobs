"""iCIMS career portals: the search page, the sitemap and each posting's page.

iCIMS is often JS-gated. The listing is read best-effort from the public
search page (``/jobs/search?ss=1&in_iframe=1``), which server-rendered
tenants paginate with ``?pr=N``; a tenant whose search page is a JS shell
still publishes every live posting in ``/sitemap.xml``. Titles there come
from the URL slug.

Locations are REAL, not assumed: a row first tries a "City, ST" read from
its own search-row text; anything still unlocated is resolved from the
posting's detail page (``?in_iframe=1`` serves the full server-rendered
document, JSON-LD included, even on JS-shell tenants), disk-cached. Only a
located-search row that resists both keeps the search term as its label:
the server already filtered those to the area. (The fetcher once stamped
EVERY row with the label, so a global tenant's out-of-area and remote
postings all entered the store labelled local.)

``searchLocation`` is a free-text query some tenants don't parse (a state
abbreviation gets 'No Results Found' on some), so a located search that
comes back empty is retried WITHOUT the location param.
"""

import json
import re
import time
from urllib.parse import unquote

from bs4 import BeautifulSoup

import config

from ..http import SESSION, HEADERS
from ..util import LOC_TEXT_RE, cache_dir
from .board import board_jobs, loc_ok
from .jsonld import (_normalize_description, _normalize_location,
                     extract_jsonld, is_jobposting)

# iCIMS's WAF 405s Chrome-like UAs that arrive without Chrome's client-hint
# headers (sec-ch-ua etc.), i.e. exactly what a requests session claiming
# Chrome looks like. A plain Mozilla platform UA passes.
ICIMS_HEADERS = {**HEADERS,
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_LOC_CACHE_TTL = 7 * 24 * 3600
_META_CAP = 150            # detail GETs per fetch call, tops

# The profile's state abbreviation ("NC", "CA"): the default searchLocation
# term and the label a located-search row keeps when nothing names its
# place. "" when the profile configures no locality.
LOCAL_LABEL = (config.LOCALITY_STATE_SUFFIX or [""])[0].upper()


def job_meta(url, need_desc=False):
    """(location, description) for one posting from its own detail page.
    Location is disk-cached (postings rarely move); description is not, so
    pass need_desc=True to force a fetch past the location cache.
    Returns ('', '') on a miss."""
    m = re.search(r"/jobs/(\d+)/", url or "")
    if not m:
        return "", ""
    cache = cache_dir("icimsloc")
    host = re.sub(r"^https?://", "", url).split("/")[0]
    p = cache / f"{host}_{m.group(1)}.json"
    cached_loc = None
    try:
        if time.time() - p.stat().st_mtime <= _LOC_CACHE_TTL:
            cached_loc = json.loads(p.read_text("utf-8")).get("loc", "")
    except Exception:
        pass
    sep = "&" if "?" in url else "?"
    detail_url = url if "in_iframe" in url else f"{url}{sep}in_iframe=1"
    if cached_loc is not None and not need_desc:
        return cached_loc, ""    # description fetched on demand by hydration
    loc = desc = ""
    try:
        r = SESSION.get(detail_url, headers=ICIMS_HEADERS)
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


def clean_title(text):
    r"""Strip the screen-reader label iCIMS row anchors put ahead of the
    title and collapse the whitespace around it. Templates differ: some
    label the anchor "Requisition Title", others just "Title" on its own
    line (three tenants stored 15 rows as "Title \n \nSr. Process Engineer"
    on 2026-09-01). A bare "Title" is only a label when a line break
    follows it, so "Title IX Coordinator" survives.

    >>> clean_title("Requisition Title Data Engineer")
    'Data Engineer'
    >>> clean_title("Title \n \nSr. Process Engineer")
    'Sr. Process Engineer'
    >>> clean_title("Title IX Coordinator")
    'Title IX Coordinator'
    >>> clean_title("  Software   Developer ")
    'Software Developer'
    """
    text = re.sub(r"^\s*Requisition Title\s*", "", text or "")
    text = re.sub(r"^\s*Title\s*\n\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url):
    """One stored URL per posting: path only, plus the `in_iframe=1` flag
    every consumer needs (it selects the server-rendered document).
    Tenants hand out the same posting under varying query strings
    (`?hub=9&in_iframe=1` from a portal alias vs `?in_iframe=1` from the
    sitemap), and upsert_job's re-key matches URLs exactly, so the variants
    became duplicate rows (2026-09-01).

    >>> canonical_url("https://careers-x.icims.com/jobs/42453/software-developer/job?hub=9&in_iframe=1")
    'https://careers-x.icims.com/jobs/42453/software-developer/job?in_iframe=1'
    >>> canonical_url("https://careers-x.icims.com/jobs/42453/software-developer/job")
    'https://careers-x.icims.com/jobs/42453/software-developer/job?in_iframe=1'
    """
    base = (url or "").split("#", 1)[0].split("?", 1)[0]
    return f"{base}?in_iframe=1" if base else ""


def host_tenant(url, default):
    """The tenant token from a posting URL's own host, which is the stable
    id namespace: a company can be configured under a portal alias
    (globalcareers-x) whose postings still live at careers-x.icims.com,
    and keying ids on the alias forked every posting into a second row.

    >>> host_tenant("https://careers-x.icims.com/jobs/1/x/job", "globalcareers-x")
    'careers-x'
    >>> host_tenant("/jobs/1/x/job", "globalcareers-x")
    'globalcareers-x'
    """
    m = re.match(r"https?://([a-z0-9-]+)\.icims\.com", url or "", re.I)
    return m.group(1).lower() if m else default


def _search_rows(tenant, params, located, loc_label):
    """The posting rows on one search page (see the module doc)."""
    found = []
    r = SESSION.get(f"https://{tenant}.icims.com/jobs/search?ss=1&in_iframe=1"
                    + params, headers=ICIMS_HEADERS)
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.select("a.iCIMS_Anchor, a[href*='/jobs/']"):
        # Row anchors carry a screen-reader label ("Requisition Title",
        # or a bare "Title" line) ahead of the actual title text.
        title = clean_title(a.get_text(" "))
        href = a.get("href", "")
        jid = re.search(r"/jobs/(\d+)/", href)
        # A numbered detail URL is what distinguishes a posting row from
        # the search shell's own chrome (/jobs/intro, /jobs/search links);
        # junk rows here would mask the sitemap fallback.
        if not title or not jid:
            continue
        row = a.find_parent()
        row_text = row.get_text(" ") if row else ""
        lm = LOC_TEXT_RE.search(row_text)
        loc = (lm.group(0).strip() if lm else (loc_label if located else ""))
        url = href if href.startswith("http") else f"https://{tenant}.icims.com{href}"
        found.append({"id": f"icims_{host_tenant(url, tenant)}_{jid.group(1)}",
                      "title": title, "url": canonical_url(url),
                      "location": loc, "description": ""})
    return found


def _sitemap_rows(tenant):
    """Every live posting a JS-shell tenant lists in /sitemap.xml; titles
    from the URL slug, locations resolved per job by the caller."""
    r = SESSION.get(f"https://{tenant}.icims.com/sitemap.xml", headers=ICIMS_HEADERS)
    out = []
    for u in re.findall(r"<loc>([^<]+)</loc>", r.text):
        m = re.search(r"/jobs/(\d+)/([^/]+)/job", u)
        if not m:
            continue
        title = unquote(m.group(2)).replace("---", " - ").replace("-", " ").strip()
        out.append({"id": f"icims_{host_tenant(u, tenant)}_{m.group(1)}",
                    "title": title, "url": canonical_url(u),
                    "location": "", "description": ""})
    return out


def fetch_icims_all(tenant, loc_re=None, loc_label=LOCAL_LABEL,
                    search_location=LOCAL_LABEL):
    """Every posting on the tenant's board that passes `loc_re`, located as
    the module doc describes. Returns [] on any failure.

    `search_location` narrows the board server-side even when `loc_re` is
    None; pass None (or "") for a whole-board pull. Rows a resolved detail
    page supplied a description for carry it; the rest are hydrated later.
    """
    out = []
    try:
        # search_location=None/"" is a whole-board pull: skip the located
        # search entirely rather than send the literal "None".
        out = (_search_rows(tenant, f"&searchLocation={search_location}",
                            True, loc_label)
               if search_location else [])
        if not out:
            for page in range(1, 9):   # locationless, paged
                batch = _search_rows(tenant, f"&pr={page - 1}" if page > 1 else "",
                                     False, loc_label)
                if not batch:
                    break
                out.extend(batch)
        if not out:
            out = _sitemap_rows(tenant)
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
            if not j["location"] and n_meta < _META_CAP:
                loc, desc = job_meta(j["url"])
                n_meta += 1
                if loc:
                    j["location"] = loc
                if desc and not j["description"]:
                    j["description"] = desc
        if loc_re is not None:
            out = [j for j in out
                   if loc_ok(loc_re, j["location"])
                   or (not j["location"] and loc_ok(loc_re, j["title"]))]
    except Exception as e:
        print(f"    [!] icims {tenant}: {e}")
    return out


def fetch_icims(tenant, company_name="", gate=None, loc_re=None,
                max_details=40, detail_delay=0.2, **listing_kw):
    """`fetch_icims_all`'s rows through the board driver (fetchers/board.py):
    `gate` screens titles and then, within `max_details` page GETs,
    descriptions. `listing_kw` reaches fetch_icims_all."""
    rows = fetch_icims_all(tenant, loc_re=loc_re, **listing_kw)
    return board_jobs(rows, company_name, gate=gate,
                      fetch_description=lambda row: job_meta(row["url"], need_desc=True)[1],
                      max_details=max_details, detail_delay=detail_delay)
