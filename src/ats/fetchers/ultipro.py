"""UKG Pro (UltiPro) recruiting job-board fetcher.

UKG Pro boards live at ``recruiting2.ultipro.com/<CODE>/JobBoard/<GUID>/`` (a
client-rendered SPA) and expose a public JSON search API:

    POST recruiting2.ultipro.com/<CODE>/JobBoard/<GUID>/JobBoardView/LoadSearchResults
      body {"opportunitySearch": {"Top", "Skip", "QueryString", "OrderBy": [], "Filters": []}}
      -> {"opportunities": [{Id, Title, Locations, BriefDescription, ...}], "totalCount"}

The listing carries the ``BriefDescription`` inline, so no per-job detail call
is needed. The store slug is ``"<CODE>|<GUID>"``.

Notes:
    parse_board posts through a bare requests.Session rather than the shared
    PoliteSession (as it has since the fetcher was added), so it names its
    timeout explicitly instead of inheriting the session default.
"""

import sys
import time

import requests
from bs4 import BeautifulSoup

from src.net.http import DEFAULT_TIMEOUT, HEADERS
from .board import board_jobs

_JSON = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json"}


def _api(slug):
    code, _, guid = slug.partition("|")
    return f"https://recruiting2.ultipro.com/{code}/JobBoard/{guid}/JobBoardView/LoadSearchResults"


def parse_board(slug, page_size=100, max_pages=10, timeout=DEFAULT_TIMEOUT):
    """Return the raw opportunity list for one board slug (``CODE|GUID``)."""
    url = _api(slug)
    out = []
    with requests.Session() as s:
        for page in range(max_pages):
            body = {"opportunitySearch": {"Top": page_size, "Skip": page * page_size,
                                          "QueryString": "", "OrderBy": [], "Filters": []}}
            r = s.post(url, json=body, timeout=timeout, headers=_JSON)
            r.raise_for_status()
            opps = r.json().get("opportunities", []) or []
            if not opps:
                break
            out.extend(opps)
            if len(opps) < page_size:
                break
            time.sleep(0.3)
    return out


def location_str(opp):
    locs = opp.get("Locations") or []
    if not locs:
        return "Unknown"
    l = locs[0]
    addr = l.get("Address") or {}
    st = addr.get("State")
    st = st.get("Code") if isinstance(st, dict) else st
    cs = ", ".join(x for x in (addr.get("City"), st) if x)
    return cs or l.get("LocalizedName") or "Unknown"


def _desc(opp):
    return BeautifulSoup(opp.get("BriefDescription") or "", "html.parser").get_text(" ", strip=True)


def _detail_url(slug, oid):
    code, _, guid = slug.partition("|")
    return (f"https://recruiting2.ultipro.com/{code}/JobBoard/{guid}"
            f"/OpportunityDetail?opportunityId={oid}")


def _row(slug, code, o):
    title = (o.get("Title") or "").strip()
    oid = o.get("Id") or ""
    if not title or not oid:
        return None
    return {"id": f"ultipro_{code}_{oid[:12]}", "title": title,
            "url": _detail_url(slug, oid), "location": location_str(o),
            "description": _desc(o)}


def fetch_ultipro(slug, company_name="", gate=None, loc_re=None):
    code = slug.split("|")[0]
    try:
        opps = parse_board(slug)
    except Exception as e:
        # Single write, not print(): this runs on fetch worker threads, and
        # print()'s separate text/newline writes let a concurrently printing
        # thread splice its line into the middle of this one (seen fused with
        # a [SNIFF] line in the 2026-08-28 discover session log).
        sys.stdout.write(f"    [!] UltiPro {company_name or code}: {e}\n")
        return []
    return board_jobs((_row(slug, code, o) for o in opps), company_name,
                      gate=gate, loc_re=loc_re)
