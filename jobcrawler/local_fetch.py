"""
NC-only job fetchers for the LOCAL-TECH crawl.

Unlike jobcrawler/fetchers/* (which pre-filter by the neuro-tuned is_relevant),
these pull ALL of a *mission-vetted* company's NC-area postings, since the
company was already vetted at discovery time. Returned job dicts:
    {id, title, url, location, description, ats, _wd}
Workday descriptions need a per-job detail call, so those are hydrated lazily
(only after the cheap technical pre-filter) via hydrate_description().
"""

import re

import requests
from bs4 import BeautifulSoup

from .http import HEADERS
from .nc import is_nc as _is_nc  # single source of truth for NC locality


def fetch_greenhouse_nc(slug):
    out = []
    try:
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                         timeout=25, headers=HEADERS)
        for j in r.json().get("jobs", []):
            loc = j.get("location", {}).get("name", "")
            if not _is_nc(loc):
                continue
            desc = BeautifulSoup(j.get("content", "") or "", "html.parser").get_text(" ")
            out.append({"id": f"gh_{slug}_{j.get('id')}", "title": j.get("title", ""),
                        "url": j.get("absolute_url", ""), "location": loc,
                        "description": desc[:4000], "ats": "greenhouse", "_wd": None})
    except Exception as e:
        print(f"    [!] greenhouse {slug}: {e}")
    return out


def fetch_lever_nc(slug):
    out = []
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                         timeout=25, headers=HEADERS)
        for j in r.json():
            loc = j.get("categories", {}).get("location", "")
            if not _is_nc(loc):
                continue
            out.append({"id": f"lv_{slug}_{j.get('id')}", "title": j.get("text", ""),
                        "url": j.get("hostedUrl", ""), "location": loc,
                        "description": (j.get("descriptionPlain") or "")[:4000],
                        "ats": "lever", "_wd": None})
    except Exception as e:
        print(f"    [!] lever {slug}: {e}")
    return out


def fetch_ashby_nc(slug):
    out = []
    try:
        r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                         timeout=25, headers=HEADERS)
        for j in r.json().get("jobPostings", []):
            loc = j.get("location", "") or ""
            if not _is_nc(loc):
                continue
            out.append({"id": f"ashby_{slug}_{j.get('id')}", "title": j.get("title", ""),
                        "url": j.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{j.get('id')}",
                        "location": loc, "description": (j.get("descriptionPlain") or "")[:4000],
                        "ats": "ashby", "_wd": None})
    except Exception as e:
        print(f"    [!] ashby {slug}: {e}")
    return out


def fetch_workday_nc(tenant, pod, site, page_size=20, max_pages=60):
    """List NC postings (title/location/path only). Descriptions hydrated later."""
    host = f"https://{tenant}.wd{pod}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    link = f"{host}/en-US/{site}"
    hdr = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json"}
    out = []
    for page in range(max_pages):
        try:
            r = requests.post(api, json={"appliedFacets": {}, "limit": page_size,
                                         "offset": page * page_size,
                                         "searchText": "North Carolina"},
                              timeout=25, headers=hdr)
            posts = r.json().get("jobPostings", []) or []
        except Exception as e:
            print(f"    [!] workday {tenant} p{page}: {e}")
            break
        if not posts:
            break
        for p in posts:
            loc = p.get("locationsText", "") or ""
            if not _is_nc(loc):
                continue
            path = p.get("externalPath", "") or ""
            jid = path.rsplit("/", 1)[-1] if path else str(abs(hash(p.get("title", "") + loc)))
            out.append({"id": f"wd_{tenant}_{jid}", "title": p.get("title", ""),
                        "url": f"{link}{path}" if path else host, "location": loc,
                        "description": "", "ats": "workday",
                        "_wd": (tenant, pod, site, path)})
        if len(posts) < page_size:
            break
    return out


def fetch_smartrecruiters_nc(slug, max_pages=10):
    """SmartRecruiters public postings API. Descriptions hydrated lazily."""
    out = []
    for page in range(max_pages):
        try:
            r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
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
            if not _is_nc(loc_s):
                continue
            pid = p.get("id")
            out.append({"id": f"sr_{slug}_{pid}", "title": p.get("name", ""),
                        "url": f"https://jobs.smartrecruiters.com/{slug}/{pid}",
                        "location": loc_s, "description": "", "ats": "smartrecruiters",
                        "_wd": None, "_sr": (slug, pid)})
        if len(content) < 100:
            break
    return out


def fetch_icims_nc(tenant):
    """
    Best-effort iCIMS scrape via the public job-search page. iCIMS is often
    JS-gated; this catches the server-rendered rows and returns [] otherwise.
    """
    out = []
    url = f"https://{tenant}.icims.com/jobs/search?ss=1&in_iframe=1&searchLocation=NC"
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a.iCIMS_Anchor, a[href*='/jobs/']"):
            title = a.get_text(" ").strip()
            href = a.get("href", "")
            if not title or "/jobs/" not in href:
                continue
            row = a.find_parent()
            loc = row.get_text(" ") if row else ""
            if not _is_nc(loc) and not _is_nc(title):
                continue
            jid = re.search(r"/jobs/(\d+)/", href)
            out.append({"id": f"icims_{tenant}_{jid.group(1) if jid else abs(hash(href))}",
                        "title": title, "url": href if href.startswith("http")
                        else f"https://{tenant}.icims.com{href}", "location": "NC",
                        "description": "", "ats": "icims", "_wd": None})
    except Exception as e:
        print(f"    [!] icims {tenant}: {e}")
    return out


def hydrate_description(job):
    """Fetch a job's real description (in place) for ATSes with a detail call."""
    if job.get("description"):
        return job
    if job.get("ats") == "workday" and job.get("_wd"):
        tenant, pod, site, path = job["_wd"]
        api = f"https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job{path}"
        try:
            r = requests.get(api, timeout=20, headers={**HEADERS, "Accept": "application/json"})
            html = r.json().get("jobPostingInfo", {}).get("jobDescription", "") or ""
            job["description"] = BeautifulSoup(html, "html.parser").get_text(" ")[:4000]
        except Exception:
            pass
    elif job.get("ats") == "smartrecruiters" and job.get("_sr"):
        slug, pid = job["_sr"]
        try:
            r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}",
                             timeout=20, headers=HEADERS)
            secs = r.json().get("jobAd", {}).get("sections", {}) or {}
            parts = [secs.get(k, {}).get("text", "") for k in
                     ("jobDescription", "qualifications", "additionalInformation")]
            html = " ".join(p for p in parts if p)
            job["description"] = BeautifulSoup(html, "html.parser").get_text(" ")[:4000]
        except Exception:
            pass
    return job


def _adapt(jobs, ats):
    """Normalize an existing fetcher's dicts to the local_fetch shape."""
    out = []
    for j in jobs:
        if not _is_nc(j.get("location", "")):
            continue
        out.append({"id": j["id"], "title": j.get("title", ""), "url": j.get("url", ""),
                    "location": j.get("location", ""), "description": j.get("description", ""),
                    "ats": ats, "_wd": None})
    return out


def fetch_successfactors_nc(base_url):
    # Duke's board is huge; reuse the existing fetcher (keyword-gated by the
    # broadened CORE_KEYWORDS the driver sets) rather than pulling everything.
    from .fetchers import fetch_successfactors
    return _adapt(fetch_successfactors("", base_url), "successfactors")


def fetch_peopleadmin_nc(host):
    from .fetchers import fetch_peopleadmin
    return _adapt(fetch_peopleadmin(host, ""), "peopleadmin")


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
# City, ST  |  City, State  |  Remote  |  an NC token
_LOC_RE = re.compile(r"[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+)*,\s*"
                     r"(?:[A-Z]{2}|[A-Z][a-z]+)\b|\bremote\b", re.I)
_OPENINGS_HREF_RE = re.compile(
    r"/(open-positions|open-roles|career-opportunities|current-openings|"
    r"job-openings|openings|opportunities|positions|jobs)\b", re.I)


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
# (aggregators are handled by Indeed ingestion; ATS hosts by _detect).
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
        if not re.match(rf"https?://{re.escape(host)}(?:/|$)", absu):
            continue  # off-domain — skip
        if _OFFSITE_RE.search(absu):
            continue
        text = a.get_text(" ", strip=True).lower()
        if _OPENINGS_HREF_RE.search(href) or re.search(
                r"(current|open|view|see|all).{0,12}(opening|position|role|job)", text):
            return absu
    return None


def _location_near(a):
    """
    Best-effort location for a job link: search the link then its container.
    Prefers an NC location when the container is multi-location (so a role
    listed "Alameda, CA | Durham, NC" is kept as a Durham job).
    """
    for el in (a, a.parent, a.parent.parent if a.parent else None):
        if el is None:
            continue
        text = el.get_text(" ", strip=True)
        if _is_nc(text):
            m = re.search(r"(?:durham|raleigh|research triangle park|research triangle|"
                          r"morrisville|chapel hill|\bcary\b|holly springs|clayton|apex)"
                          r"(?:[,\s]+(?:nc|north carolina))?", text, re.I)
            return m.group(0) if m else "NC"
        m = _LOC_RE.search(text)
        if m:
            return m.group(0)
    return ""


def _get_soup(url):
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        if r.status_code != 200:
            return None
        return BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None


def fetch_custom_careers_nc(careers_url, _hop=True):
    """
    Scrape a self-hosted / custom careers board (no standard ATS) for NC jobs.
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
            return fetch_custom_careers_nc(op, _hop=False)
    out, seen = [], set()
    for a, href, title in links:
        loc = _location_near(a)
        if not _is_nc(loc):
            continue
        url = href if href.startswith("http") else root + href
        if url in seen:
            continue
        seen.add(url)
        out.append({"id": f"custom_{re.sub(r'[^a-z0-9]+', '-', url.lower())[-48:]}",
                    "title": title[:90], "url": url, "location": loc[:70],
                    "description": "", "ats": "custom", "_wd": None})
    return out


def custom_board_listing_url(page_url, html=None):
    """
    If `page_url` (or the openings page it links to, one hop) is a real custom
    job board (>=3 genuine job-detail links, not nav), return the URL that holds
    the listings; else None. Used by the sniffer to detect + resolve the board.
    """
    if _OFFSITE_RE.search(page_url):
        return None  # aggregator/ATS host is never a company's own custom board
    root = re.match(r"https?://[^/]+", page_url).group(0)
    soup = BeautifulSoup(html, "html.parser") if html is not None else _get_soup(page_url)
    if soup is None:
        return None
    if len(find_job_links(soup)) >= 3:
        return page_url
    op = _openings_link(soup, root)
    if op and op.rstrip("/") != page_url.rstrip("/"):
        s2 = _get_soup(op)
        if s2 and len(find_job_links(s2)) >= 3:
            return op
    return None


FETCHERS = {
    "greenhouse":      lambda c: fetch_greenhouse_nc(c["slug"]),
    "lever":           lambda c: fetch_lever_nc(c["slug"]),
    "ashby":           lambda c: fetch_ashby_nc(c["slug"]),
    "workday":         lambda c: fetch_workday_nc(c["wd_tenant"], c["wd_pod"], c["wd_site"]),
    "smartrecruiters": lambda c: fetch_smartrecruiters_nc(c["slug"]),
    "icims":           lambda c: fetch_icims_nc(c["slug"]),
    "successfactors":  lambda c: fetch_successfactors_nc(c["careers_url"]),
    "peopleadmin":     lambda c: fetch_peopleadmin_nc(c["careers_url"]),
    "custom":          lambda c: fetch_custom_careers_nc(c["careers_url"]),
}


def fetch_company_nc(company):
    """Dispatch to the right NC fetcher for a company dict from the store."""
    fn = FETCHERS.get(company.get("ats"))
    return fn(company) if fn else []
