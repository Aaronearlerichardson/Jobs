"""ATS platforms with a clean public JSON API: Greenhouse, Lever, Ashby."""

import requests
from bs4 import BeautifulSoup

from ..filters import is_relevant
from ..http import HEADERS


def fetch_greenhouse(slug, company_name):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"    [!] Greenhouse {company_name}: {e}")
        return []
    if not isinstance(data, dict):
        return []
    jobs = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        jid   = str(job.get("id", ""))
        jurl  = job.get("absolute_url", "")
        loc   = job.get("location", {}).get("name", "Unknown")
        desc  = BeautifulSoup(job.get("content", ""), "html.parser").get_text(" ")
        dept  = " ".join(d.get("name", "") for d in job.get("departments", []))
        offices = " ".join((o.get("name") or "")
                           for o in job.get("offices", []) or [])
        if is_relevant(f"{title} {dept}", desc):
            rec = {"id": f"gh_{slug}_{jid}", "company": company_name,
                   "title": title, "url": jurl, "location": loc,
                   "description": desc}
            if "remote" in offices.lower():
                rec["remote_hint"] = "greenhouse:office"
            jobs.append(rec)
    return jobs


def fetch_lever(slug, company_name):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"    [!] Lever {company_name}: {e}")
        return []
    if not isinstance(data, list):
        return []
    jobs = []
    for job in data:
        title = job.get("text", "")
        jid   = job.get("id", "")
        jurl  = job.get("hostedUrl", "")
        loc   = job.get("categories", {}).get("location", "Unknown")
        team  = job.get("categories", {}).get("team", "")
        desc  = job.get("descriptionPlain") or ""
        if is_relevant(f"{title} {team}", desc):
            rec = {"id": f"lv_{slug}_{jid}", "company": company_name,
                   "title": title, "url": jurl, "location": loc,
                   "description": desc}
            if str(job.get("workplaceType", "")).lower() == "remote":
                rec["remote_hint"] = "lever:workplaceType"
            jobs.append(rec)
    return jobs


def fetch_ashby(slug, company_name):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"    [!] Ashby {company_name}: {e}")
        return []
    if not isinstance(data, dict):
        return []
    jobs = []
    for job in data.get("jobPostings", []):
        title = job.get("title", "")
        jid   = job.get("id", "")
        jurl  = job.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{jid}"
        loc   = job.get("location", "Unknown") or "Unknown"
        dept  = job.get("departmentName", "")
        desc  = job.get("descriptionPlain", "") or ""
        if is_relevant(f"{title} {dept}", desc):
            rec = {"id": f"ashby_{slug}_{jid}", "company": company_name,
                   "title": title, "url": jurl, "location": loc,
                   "description": desc}
            if job.get("isRemote") is True:
                rec["remote_hint"] = "ashby:isRemote"
            jobs.append(rec)
    return jobs
