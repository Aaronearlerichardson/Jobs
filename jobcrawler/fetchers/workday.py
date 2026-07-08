"""Workday career-site JSON endpoint."""

import time

import requests

from ..filters import is_relevant
from ..http import HEADERS


def fetch_workday(tenant, wd_pod, site, company_name, page_size=20, max_pages=25):
    """
    Poll a Workday career site via the JSON POST endpoint.
      POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
      body: {"appliedFacets":{},"limit":N,"offset":K,"searchText":""}
    """
    host = f"https://{tenant}.wd{wd_pod}.myworkdayjobs.com"
    api  = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    link_base = f"{host}/en-US/{site}"

    wd_headers = {
        **HEADERS,
        "Accept":       "application/json",
        "Content-Type": "application/json",
    }

    jobs = []
    for page in range(max_pages):
        offset = page * page_size
        body = {
            "appliedFacets": {},
            "limit":         page_size,
            "offset":        offset,
            "searchText":    "",
        }
        try:
            r = requests.post(api, json=body, timeout=25, headers=wd_headers)
            r.raise_for_status()
        except Exception as e:
            print(f"    [!] Workday {company_name} p{page}: {e}")
            break

        try:
            data = r.json()
        except ValueError:
            print(f"    [!] Workday {company_name}: non-JSON response")
            break

        postings = data.get("jobPostings", []) or []
        if not postings:
            break

        for p in postings:
            title  = p.get("title", "") or ""
            path   = p.get("externalPath", "") or ""
            jurl   = f"{link_base}{path}" if path else host
            loc    = p.get("locationsText", "") or "Unknown"
            posted = p.get("postedOn", "") or ""
            jid    = path.rsplit("/", 1)[-1] if path else str(abs(hash(title + loc)))
            desc   = posted
            if is_relevant(title, desc):
                jobs.append({
                    "id":          f"wd_{tenant}_{jid}",
                    "company":     company_name,
                    "title":       title,
                    "url":         jurl,
                    "location":    loc,
                    "description": desc,
                })

        if len(postings) < page_size:
            break
        time.sleep(0.5)

    return jobs
