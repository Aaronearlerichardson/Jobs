"""UKG Pro (UltiPro) recruiting job-board fetcher.

UKG Pro boards live at ``recruiting2.ultipro.com/<CODE>/JobBoard/<GUID>/`` (a
client-rendered SPA) and expose a public JSON search API:

    POST recruiting2.ultipro.com/<CODE>/JobBoard/<GUID>/JobBoardView/LoadSearchResults
      body {"opportunitySearch": {"Top", "Skip", "QueryString", "OrderBy": [], "Filters": []}}
      -> {"opportunities": [{Id, Title, Locations, BriefDescription, ...}], "totalCount"}

The listing carries the ``BriefDescription`` inline, so no per-job detail call
is needed. The store slug is ``"<CODE>|<GUID>"``.
"""

import time

import requests
from bs4 import BeautifulSoup

from ..filters import is_relevant
from ..http import HEADERS

_JSON = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json"}


def _api(slug):
    code, _, guid = slug.partition("|")
    return f"https://recruiting2.ultipro.com/{code}/JobBoard/{guid}/JobBoardView/LoadSearchResults"


def parse_board(slug, page_size=100, max_pages=10, timeout=25):
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


def fetch_ultipro(slug, company_name):
    """Keyword-gated fetch. The BriefDescription is inline, so title+desc gate
    directly with no per-job detail call."""
    try:
        opps = parse_board(slug)
    except Exception as e:
        print(f"    [!] UltiPro {company_name}: {e}")
        return []
    code = slug.split("|")[0]
    out = []
    for o in opps:
        title = (o.get("Title") or "").strip()
        oid = o.get("Id") or ""
        if not title or not oid:
            continue
        desc = _desc(o)
        if not is_relevant(title, desc):
            continue
        out.append({"id": f"ultipro_{code}_{oid[:12]}", "company": company_name,
                    "title": title, "url": _detail_url(slug, oid),
                    "location": location_str(o), "description": desc})
    return out
