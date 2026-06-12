"""BambooHR public job-board fetcher.

BambooHR-hosted boards live at ``https://<subdomain>.bamboohr.com/careers``
and expose two unauthenticated JSON endpoints:

    /careers/list          -> {"result": [{id, jobOpeningName,
                               departmentLabel, employmentStatusLabel,
                               location{city,state}, isRemote,
                               locationType}, ...]}
    /careers/<id>/detail   -> {"result": {"jobOpening": {description,
                               jobOpeningShareUrl, datePosted, ...}}}

The list is cheap; descriptions cost one request per job, so they're
fetched only for list entries that already look relevant from the title.
locationType "1" / a truthy isRemote marks remote roles (structured
signal, stamped as remote_hint).
"""

import time

import requests
from bs4 import BeautifulSoup

from ..filters import is_relevant
from ..http import HEADERS

_JSON_HEADERS = {**HEADERS, "Accept": "application/json"}


def _location_str(job):
    loc = job.get("location") or {}
    parts = [loc.get("city"), loc.get("state")]
    joined = ", ".join(p for p in parts if p)
    if _is_remote(job):
        return f"Remote{' / ' + joined if joined else ''}"
    return joined or "Unknown"


def _is_remote(job):
    return bool(job.get("isRemote")) or str(job.get("locationType")) == "1"


def _fetch_description(base, jid, timeout=15):
    try:
        r = requests.get(f"{base}/careers/{jid}/detail",
                         timeout=timeout, headers=_JSON_HEADERS)
        r.raise_for_status()
        opening = (r.json().get("result") or {}).get("jobOpening") or {}
        html = opening.get("description") or ""
        return BeautifulSoup(html, "html.parser").get_text(" ")
    except Exception:
        return ""


def fetch_bamboohr(subdomain, company_name, max_details=40, detail_delay=0.2):
    base = f"https://{subdomain}.bamboohr.com"
    try:
        r = requests.get(f"{base}/careers/list", timeout=20,
                         headers=_JSON_HEADERS)
        r.raise_for_status()
        entries = r.json().get("result") or []
    except Exception as e:
        print(f"    [!] BambooHR {company_name}: {e}")
        return []

    jobs, details_fetched = [], 0
    for entry in entries:
        jid   = str(entry.get("id") or "")
        title = entry.get("jobOpeningName") or ""
        if not jid or not title:
            continue
        dept = entry.get("departmentLabel") or ""

        # Title/department screen first; fetch the description only when
        # the cheap fields didn't already decide relevance.
        desc = ""
        if not is_relevant(f"{title} {dept}") and details_fetched < max_details:
            desc = _fetch_description(base, jid)
            details_fetched += 1
            time.sleep(detail_delay)
        if not is_relevant(f"{title} {dept}", desc):
            continue
        if not desc and details_fetched < max_details:
            desc = _fetch_description(base, jid)
            details_fetched += 1
            time.sleep(detail_delay)

        job = {
            "id":          f"bamboo_{subdomain}_{jid}",
            "company":     company_name,
            "title":       title,
            "url":         f"{base}/careers/{jid}",
            "location":    _location_str(entry),
            "description": desc,
        }
        if _is_remote(entry):
            job["remote_hint"] = "bamboohr:locationType"
        jobs.append(job)
    return jobs
