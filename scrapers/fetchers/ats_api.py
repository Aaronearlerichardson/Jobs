"""ATS platforms with a clean public JSON API: Greenhouse, Lever, Ashby."""

from bs4 import BeautifulSoup

from core.filters import is_relevant
from ..http import SESSION, HEADERS


def fetch_greenhouse(slug, company_name):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        r = SESSION.get(url, timeout=20, headers=HEADERS)
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
        r = SESSION.get(url, timeout=20, headers=HEADERS)
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
        r = SESSION.get(url, timeout=20, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"    [!] Ashby {company_name}: {e}")
        return []
    if not isinstance(data, dict):
        return []
    jobs = []
    # The posting-api returns {"jobs": [...], "apiVersion": ...}. This read
    # "jobPostings" and the department field "departmentName", so EVERY
    # Ashby board silently yielded zero postings (a missing key is an empty
    # list, and the caller can't tell that from "no matches"). Caught by the
    # board canary — see tools/check_boards.py.
    for job in data.get("jobs", []):
        title = job.get("title", "")
        jid   = job.get("id", "")
        jurl  = job.get("jobUrl", "") or f"https://jobs.ashbyhq.com/{slug}/{jid}"
        loc   = job.get("location", "Unknown") or "Unknown"
        dept  = " ".join(x for x in (job.get("department"), job.get("team")) if x)
        desc  = job.get("descriptionPlain", "") or ""
        if is_relevant(f"{title} {dept}", desc):
            rec = {"id": f"ashby_{slug}_{jid}", "company": company_name,
                   "title": title, "url": jurl, "location": loc,
                   "description": desc,
                   "posted_at": job.get("publishedAt")}
            # workplaceType is the structured signal; isRemote is the older
            # boolean. Either one beats regexing the location string.
            if job.get("isRemote") is True or job.get("workplaceType") == "Remote":
                rec["remote_hint"] = "ashby:isRemote"
            jobs.append(rec)
    return jobs
