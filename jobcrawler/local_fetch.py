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

_NC = re.compile(r"\bnc\b|north carolina|durham|raleigh|chapel hill|morrisville|"
                 r"\bcary\b|research triangle|\brtp\b|holly springs|clayton|"
                 r"franklinton|burlington|\bapex\b|wake forest", re.I)


def _is_nc(text):
    return bool(_NC.search(text or ""))


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


FETCHERS = {
    "greenhouse":      lambda c: fetch_greenhouse_nc(c["slug"]),
    "lever":           lambda c: fetch_lever_nc(c["slug"]),
    "ashby":           lambda c: fetch_ashby_nc(c["slug"]),
    "workday":         lambda c: fetch_workday_nc(c["wd_tenant"], c["wd_pod"], c["wd_site"]),
    "smartrecruiters": lambda c: fetch_smartrecruiters_nc(c["slug"]),
    "icims":           lambda c: fetch_icims_nc(c["slug"]),
    "successfactors":  lambda c: fetch_successfactors_nc(c["careers_url"]),
    "peopleadmin":     lambda c: fetch_peopleadmin_nc(c["careers_url"]),
}


def fetch_company_nc(company):
    """Dispatch to the right NC fetcher for a company dict from the store."""
    fn = FETCHERS.get(company.get("ats"))
    return fn(company) if fn else []
