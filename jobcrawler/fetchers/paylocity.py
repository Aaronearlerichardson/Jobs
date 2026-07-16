"""Paylocity public recruiting-board fetcher.

Paylocity-hosted boards live at
``https://recruiting.paylocity.com/recruiting/jobs/All/<company-guid>/<name>``.
The board page embeds the whole listing as a ``pageData`` JSON blob (there is
no separate list API): ``pageData.Jobs[]`` carries
``{JobId, JobTitle, JobLocation{City,State,Country}, LocationName, IsRemote}``.
Each posting's full description is server-rendered on its detail page
(``/Recruiting/Jobs/Details/<JobId>`` -> ``.job-preview-details``) and fetched
lazily, the same title-screen-then-hydrate shape as the BambooHR fetcher.

The store slug is the company GUID; the name segment of the board URL is
cosmetic (the GUID-only URL returns the same data).
"""

import json
import re
import time

import requests
from bs4 import BeautifulSoup

from ..filters import is_relevant
from ..http import HEADERS

_BOARD = "https://recruiting.paylocity.com/recruiting/jobs/All/{guid}/x"
_DETAIL = "https://recruiting.paylocity.com/Recruiting/Jobs/Details/{jid}"
_PAGEDATA_RE = re.compile(r"pageData\s*=\s*(\{.*?\});", re.S)


def parse_board(guid, timeout=20):
    """Return the raw ``pageData.Jobs`` list for one board GUID."""
    r = requests.get(_BOARD.format(guid=guid), timeout=timeout, headers=HEADERS)
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


def fetch_description(job_id, timeout=15):
    """Full JD text for one posting, from its server-rendered detail page."""
    try:
        r = requests.get(_DETAIL.format(jid=job_id), timeout=timeout, headers=HEADERS)
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


def fetch_paylocity(guid, company_name, max_details=40, detail_delay=0.2):
    """Keyword-gated fetch (for sweeping unvetted boards): title-screen first,
    hydrate the description only when the title alone didn't decide relevance."""
    try:
        raw = parse_board(guid)
    except Exception as e:
        print(f"    [!] Paylocity {company_name}: {e}")
        return []
    out, fetched = [], 0
    for j in raw:
        jid = str(j.get("JobId") or "")
        title = j.get("JobTitle") or ""
        if not jid or not title:
            continue
        desc = re.sub(r"<[^>]+>", " ", j.get("Description") or "")
        if not is_relevant(title) and fetched < max_details:
            desc = fetch_description(jid)
            fetched += 1
            time.sleep(detail_delay)
        if not is_relevant(title, desc):
            continue
        if not desc and fetched < max_details:
            desc = fetch_description(jid)
            fetched += 1
            time.sleep(detail_delay)
        rec = {"id": f"paylocity_{guid[:8]}_{jid}", "company": company_name,
               "title": title, "url": _DETAIL.format(jid=jid),
               "location": location_str(j), "description": desc}
        if j.get("IsRemote"):
            rec["remote_hint"] = "paylocity:isRemote"
        out.append(rec)
    return out
