"""Paylocity public recruiting-board fetcher.

Paylocity-hosted boards live at
``https://recruiting.paylocity.com/recruiting/jobs/All/<company-guid>/<name>``.
The board page embeds the whole listing as a ``pageData`` JSON blob (there is
no separate list API): ``pageData.Jobs[]`` carries
``{JobId, JobTitle, JobLocation{City,State,Country}, LocationName, IsRemote,
Description}``. A posting whose listing entry carries no ``Description`` gets
it from its server-rendered detail page
(``/Recruiting/Jobs/Details/<JobId>`` -> ``.job-preview-details``), within
the budget fetchers/board.py describes.

The store slug is the company GUID; the name segment of the board URL is
cosmetic (the GUID-only URL returns the same data).
"""

import json
import re

from bs4 import BeautifulSoup

from src.net.http import SESSION, HEADERS
from .board import board_jobs

_BOARD = "https://recruiting.paylocity.com/recruiting/jobs/All/{guid}/x"
_DETAIL = "https://recruiting.paylocity.com/Recruiting/Jobs/Details/{jid}"
_PAGEDATA_RE = re.compile(r"pageData\s*=\s*(\{.*?\});", re.S)


def parse_board(guid, timeout=None):
    """Return the raw ``pageData.Jobs`` list for one board GUID."""
    r = SESSION.get(_BOARD.format(guid=guid), timeout=timeout, headers=HEADERS)
    r.raise_for_status()
    m = _PAGEDATA_RE.search(r.text)
    if not m:
        return []
    return json.loads(m.group(1)).get("Jobs", []) or []


def location_str(job):
    if job.get("LocationName"):
        return job["LocationName"]
    jl = job.get("JobLocation") or {}
    city_state = ", ".join(x for x in (jl.get("City"), jl.get("State")) if x)
    if city_state:
        return city_state
    if job.get("IsRemote"):
        return "Remote"
    return (jl.get("Country") or "Unknown")


def fetch_description(job_id, timeout=None):
    """Full JD text for one posting, from its server-rendered detail page."""
    try:
        r = SESSION.get(_DETAIL.format(jid=job_id), timeout=timeout, headers=HEADERS)
        r.raise_for_status()
        el = BeautifulSoup(r.text, "html.parser").select_one(
            ".job-preview-details, [class*=job-preview]")
        if not el:
            return ""
        text = el.get_text(" ", strip=True)
        # Strip the "Apply <title> <location> Apply Description" chrome that
        # leads every detail page, keeping the JD body.
        body = re.sub(r"^.*?\bDescription\b", "", text, count=1).strip()
        return body or text
    except Exception:
        return ""


def _row(guid, j):
    jid = str(j.get("JobId") or "")
    title = j.get("JobTitle") or ""
    if not jid or not title:
        return None
    row = {"id": f"paylocity_{guid[:8]}_{jid}", "title": title,
           "url": _DETAIL.format(jid=jid), "location": location_str(j),
           "description": re.sub(r"<[^>]+>", " ", j.get("Description") or ""),
           "_jid": jid}
    if j.get("IsRemote"):
        row["remote_hint"] = "paylocity:isRemote"
    return row


def fetch_paylocity(guid, company_name="", gate=None, loc_re=None, max_details=40,
                    detail_delay=0.2):
    try:
        raw = parse_board(guid)
    except Exception as e:
        print(f"    [!] Paylocity {company_name or guid[:8]}: {e}")
        return []
    return board_jobs((_row(guid, j) for j in raw), company_name,
                      gate=gate, loc_re=loc_re,
                      fetch_description=lambda row: fetch_description(row["_jid"]),
                      max_details=max_details, detail_delay=detail_delay)
