"""
Company-scoped fetchers: pull ALL of a *mission-vetted* company's postings,
optionally location-filtered.

Contrast with the sibling modules in jobcrawler/fetchers/* — those pre-filter
every posting through the keyword filter (filters.is_relevant), which is right
for sweeping unvetted boards. Here the company was already vetted (mission
scored at discovery time, stored in jobcrawler/store.py), so the whole board
is pulled and the caller's own filter chain decides.

Returned job dicts:
    {id, title, url, location, description, ats, _wd}

The location gate is a compiled regex (`loc_re`), so any track can scope the
pull: the local track passes NC_RE (Triangle/NC), a future track could pass
another region, and None pulls everything. Workday / SmartRecruiters
descriptions need a per-job detail call, so those are hydrated lazily —
only after the cheap technical pre-filter — via hydrate_description().
"""

import re

import requests
from bs4 import BeautifulSoup

from ..http import HEADERS

# Triangle/NC (incl. ~2.5h commute ring) — the local track's location gate.
NC_RE = re.compile(r"\bnc\b|north carolina|durham|raleigh|chapel hill|morrisville|"
                   r"\bcary\b|research triangle|\brtp\b|holly springs|clayton|"
                   r"franklinton|burlington|\bapex\b|wake forest", re.I)


def _loc_ok(loc_re, text):
    return loc_re is None or bool(loc_re.search(text or ""))


def fetch_greenhouse_all(slug, loc_re=None):
    out = []
    try:
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
                         timeout=25, headers=HEADERS)
        for j in r.json().get("jobs", []):
            loc = j.get("location", {}).get("name", "")
            if not _loc_ok(loc_re, loc):
                continue
            desc = BeautifulSoup(j.get("content", "") or "", "html.parser").get_text(" ")
            out.append({"id": f"gh_{slug}_{j.get('id')}", "title": j.get("title", ""),
                        "url": j.get("absolute_url", ""), "location": loc,
                        "description": desc[:4000], "ats": "greenhouse", "_wd": None})
    except Exception as e:
        print(f"    [!] greenhouse {slug}: {e}")
    return out


def fetch_lever_all(slug, loc_re=None):
    out = []
    try:
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                         timeout=25, headers=HEADERS)
        for j in r.json():
            loc = j.get("categories", {}).get("location", "")
            if not _loc_ok(loc_re, loc):
                continue
            out.append({"id": f"lv_{slug}_{j.get('id')}", "title": j.get("text", ""),
                        "url": j.get("hostedUrl", ""), "location": loc,
                        "description": (j.get("descriptionPlain") or "")[:4000],
                        "ats": "lever", "_wd": None})
    except Exception as e:
        print(f"    [!] lever {slug}: {e}")
    return out


def fetch_ashby_all(slug, loc_re=None):
    out = []
    try:
        r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                         timeout=25, headers=HEADERS)
        for j in r.json().get("jobPostings", []):
            loc = j.get("location", "") or ""
            if not _loc_ok(loc_re, loc):
                continue
            out.append({"id": f"ashby_{slug}_{j.get('id')}", "title": j.get("title", ""),
                        "url": j.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{j.get('id')}",
                        "location": loc, "description": (j.get("descriptionPlain") or "")[:4000],
                        "ats": "ashby", "_wd": None})
    except Exception as e:
        print(f"    [!] ashby {slug}: {e}")
    return out


def fetch_workday_all(tenant, pod, site, loc_re=None, search_text="North Carolina",
                      page_size=20, max_pages=60):
    """List postings (title/location/path only). Descriptions hydrated later.

    `search_text` narrows the Workday CXS search server-side (a full
    Medtronic/IQVIA board is thousands of reqs); pass "" to sweep all.
    """
    host = f"https://{tenant}.wd{pod}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    link = f"{host}/en-US/{site}"
    hdr = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json"}
    out = []
    for page in range(max_pages):
        try:
            r = requests.post(api, json={"appliedFacets": {}, "limit": page_size,
                                         "offset": page * page_size,
                                         "searchText": search_text},
                              timeout=25, headers=hdr)
            posts = r.json().get("jobPostings", []) or []
        except Exception as e:
            print(f"    [!] workday {tenant} p{page}: {e}")
            break
        if not posts:
            break
        for p in posts:
            loc = p.get("locationsText", "") or ""
            if not _loc_ok(loc_re, loc):
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


def fetch_smartrecruiters_all(slug, loc_re=None, max_pages=10):
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
            if not _loc_ok(loc_re, loc_s):
                continue
            pid = p.get("id")
            out.append({"id": f"sr_{slug}_{pid}", "title": p.get("name", ""),
                        "url": f"https://jobs.smartrecruiters.com/{slug}/{pid}",
                        "location": loc_s, "description": "", "ats": "smartrecruiters",
                        "_wd": None, "_sr": (slug, pid)})
        if len(content) < 100:
            break
    return out


def fetch_icims_all(tenant, loc_re=None, loc_label="NC", search_location="NC"):
    """
    Best-effort iCIMS scrape via the public job-search page. iCIMS is often
    JS-gated; this catches the server-rendered rows and returns [] otherwise.
    """
    out = []
    url = (f"https://{tenant}.icims.com/jobs/search?ss=1&in_iframe=1"
           f"&searchLocation={search_location}")
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
            if not _loc_ok(loc_re, loc) and not _loc_ok(loc_re, title):
                continue
            jid = re.search(r"/jobs/(\d+)/", href)
            out.append({"id": f"icims_{tenant}_{jid.group(1) if jid else abs(hash(href))}",
                        "title": title, "url": href if href.startswith("http")
                        else f"https://{tenant}.icims.com{href}", "location": loc_label,
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


def fetch_successfactors_all(base_url, loc_re=None):
    # Duke's board is huge; reuse the existing fetcher (keyword-gated by the
    # live CORE_KEYWORDS the track sets) rather than pulling everything.
    from . import fetch_successfactors
    return _adapt(fetch_successfactors("", base_url), "successfactors", loc_re)


def fetch_peopleadmin_all(host, loc_re=None):
    from . import fetch_peopleadmin
    return _adapt(fetch_peopleadmin(host, ""), "peopleadmin", loc_re)


def fetch_custom_careers(careers_url, loc_re=None):
    """
    Scrape a self-hosted / custom careers page (no standard ATS). Generic:
    finds job-detail links whose text is a title with a nearby location.
    Covers Astro/Webflow-style boards like Science Corp (science.xyz),
    which no ATS probe/sniffer can reach.
    """
    out, seen = [], set()
    try:
        r = requests.get(careers_url, timeout=20, headers=HEADERS)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"    [!] custom {careers_url}: {e}")
        return out
    root = re.match(r"https?://[^/]+", careers_url).group(0)
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not re.search(r"/(careers|positions|jobs|openings)/[a-z0-9]", href, re.I):
            continue
        te = a.select_one("h1,h2,h3,h4,h5,[class*='title']")
        title = (te.get_text(" ").strip() if te else a.get_text(" ").strip())
        le = a.select_one("[class*='description'],[class*='location'],[class*='meta']")
        loc = le.get_text(" ").strip() if le else a.get_text(" ").strip()
        if not title or not _loc_ok(loc_re, loc):
            continue
        url = href if href.startswith("http") else root + href
        if url in seen:
            continue
        seen.add(url)
        out.append({"id": f"custom_{re.sub(r'[^a-z0-9]+', '-', url.lower())[-48:]}",
                    "title": title[:90], "url": url, "location": loc[:70],
                    "description": "", "ats": "custom", "_wd": None})
    return out


FETCHERS = {
    "greenhouse":      lambda c, lr: fetch_greenhouse_all(c["slug"], lr),
    "lever":           lambda c, lr: fetch_lever_all(c["slug"], lr),
    "ashby":           lambda c, lr: fetch_ashby_all(c["slug"], lr),
    "workday":         lambda c, lr: fetch_workday_all(c["wd_tenant"], c["wd_pod"], c["wd_site"], lr),
    "smartrecruiters": lambda c, lr: fetch_smartrecruiters_all(c["slug"], lr),
    "icims":           lambda c, lr: fetch_icims_all(c["slug"], lr),
    "successfactors":  lambda c, lr: fetch_successfactors_all(c["careers_url"], lr),
    "peopleadmin":     lambda c, lr: fetch_peopleadmin_all(c["careers_url"], lr),
    "custom":          lambda c, lr: fetch_custom_careers(c["careers_url"], lr),
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
