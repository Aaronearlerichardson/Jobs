"""BambooHR public job-board fetcher.

BambooHR-hosted boards live at ``https://<subdomain>.bamboohr.com/careers``
and expose two unauthenticated JSON endpoints:

    /careers/list          -> {"result": [{id, jobOpeningName,
                               departmentLabel, employmentStatusLabel,
                               location{city,state}, isRemote,
                               locationType}, ...]}
    /careers/<id>/detail   -> {"result": {"jobOpening": {description,
                               ...}}}

The listing carries no description, so each kept row costs one detail
call (see fetchers/board.py for the order of filters and the budget).
"""

from bs4 import BeautifulSoup

from src.net.http import SESSION, HEADERS
from .board import board_jobs

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


def _fetch_description(base, jid, timeout=None):
    try:
        r = SESSION.get(f"{base}/careers/{jid}/detail",
                         timeout=timeout, headers=_JSON_HEADERS)
        r.raise_for_status()
        opening = (r.json().get("result") or {}).get("jobOpening") or {}
        html = opening.get("description") or ""
        return BeautifulSoup(html, "html.parser").get_text(" ")
    except Exception:
        return ""


def _row(base, subdomain, entry):
    jid = str(entry.get("id") or "")
    title = entry.get("jobOpeningName") or ""
    if not jid or not title:
        return None
    row = {"id": f"bamboo_{subdomain}_{jid}", "title": title,
           "url": f"{base}/careers/{jid}", "location": _location_str(entry),
           "description": "",
           "head": f"{title} {entry.get('departmentLabel') or ''}",
           "_jid": jid}
    if _is_remote(entry):
        row["remote_hint"] = "bamboohr:locationType"
    return row


def fetch_bamboohr(subdomain, company_name="", gate=None, loc_re=None,
                   max_details=40, detail_delay=0.2):
    base = f"https://{subdomain}.bamboohr.com"
    try:
        r = SESSION.get(f"{base}/careers/list", headers=_JSON_HEADERS)
        r.raise_for_status()
        entries = r.json().get("result") or []
    except Exception as e:
        print(f"    [!] BambooHR {company_name or subdomain}: {e}")
        return []
    return board_jobs((_row(base, subdomain, e) for e in entries), company_name,
                      gate=gate, loc_re=loc_re,
                      fetch_description=lambda row: _fetch_description(base, row["_jid"]),
                      max_details=max_details, detail_delay=detail_delay)
