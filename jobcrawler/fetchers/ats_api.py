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
    except Exception as e:
        print(f"    [!] Greenhouse {company_name}: {e}")
        return []
    jobs = []
    for job in r.json().get("jobs", []):
        title = job.get("title", "")
        jid   = str(job.get("id", ""))
        jurl  = job.get("absolute_url", "")
        loc   = job.get("location", {}).get("name", "Unknown")
        desc  = BeautifulSoup(job.get("content", ""), "html.parser").get_text(" ")[:600]
        dept  = " ".join(d.get("name", "") for d in job.get("departments", []))
        if is_relevant(f"{title} {dept}", desc):
            jobs.append({"id": f"gh_{slug}_{jid}", "company": company_name,
                         "title": title, "url": jurl, "location": loc, "description": desc})
    return jobs


def fetch_lever(slug, company_name):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Lever {company_name}: {e}")
        return []
    jobs = []
    for job in r.json():
        title = job.get("text", "")
        jid   = job.get("id", "")
        jurl  = job.get("hostedUrl", "")
        loc   = job.get("categories", {}).get("location", "Unknown")
        team  = job.get("categories", {}).get("team", "")
        desc  = (job.get("descriptionPlain") or "")[:600]
        if is_relevant(f"{title} {team}", desc):
            jobs.append({"id": f"lv_{slug}_{jid}", "company": company_name,
                         "title": title, "url": jurl, "location": loc, "description": desc})
    return jobs


def fetch_ashby(slug, company_name):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Ashby {company_name}: {e}")
        return []
    jobs = []
    for job in r.json().get("jobPostings", []):
        title = job.get("title", "")
        jid   = job.get("id", "")
        jurl  = job.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{jid}"
        loc   = job.get("location", "Unknown") or "Unknown"
        dept  = job.get("departmentName", "")
        desc  = job.get("descriptionPlain", "") or ""
        if is_relevant(f"{title} {dept}", desc[:600]):
            jobs.append({"id": f"ashby_{slug}_{jid}", "company": company_name,
                         "title": title, "url": jurl, "location": loc, "description": desc[:600]})
    return jobs
