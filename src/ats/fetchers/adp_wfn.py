"""ADP Workforce Now public job-requisitions fetcher.

ADP WFN career centers embed at
``workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html``
with two identifying query params: ``cid`` (a GUID) and ``ccId``. The
same params drive an unauthenticated JSON API:

    /mascsr/default/careercenter/public/events/staffing/v1/
        job-requisitions?cid=<cid>&ccId=<ccid>&locale=en_US&$top=N&$skip=K

Each requisition has itemID, requisitionTitle, postDate and
requisitionLocations; the description comes from a per-requisition detail
call (see fetchers/board.py for the order of filters and the budget). The
store slug is ``"<cid>|<ccid>"``.
"""

import time

from bs4 import BeautifulSoup

from src.net.http import JSON_HEADERS, SESSION, fetch_failed
from .board import board_jobs

_API = ("https://workforcenow.adp.com/mascsr/default/careercenter/public"
        "/events/staffing/v1/job-requisitions")
_PORTAL = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment"
           "/recruitment.html")


def _location_str(req):
    locs = req.get("requisitionLocations") or []
    names = []
    for l in locs:
        n = ((l.get("nameCode") or {}).get("shortName") or "").strip()
        if n:
            names.append(n)
    return "; ".join(names) or "Unknown"


def _fetch_description(item_id, cid, ccid, timeout=None):
    try:
        r = SESSION.get(
            f"{_API}/{item_id}",
            params={"cid": cid, "ccId": ccid, "locale": "en_US"},
            timeout=timeout, headers=JSON_HEADERS,
        )
        r.raise_for_status()
        data = r.json()
        req = (data.get("jobRequisitions") or [data])[0] \
            if isinstance(data.get("jobRequisitions"), list) else data
        html = (req.get("requisitionDescription")
                or req.get("description") or "")
        if isinstance(html, list):
            html = " ".join(str(x) for x in html)
        return BeautifulSoup(str(html), "html.parser").get_text(" ")
    except Exception:
        return ""


def _row(cid, ccid, req):
    item_id = str(req.get("itemID") or "")
    title = req.get("requisitionTitle") or ""
    if not item_id or not title:
        return None
    return {"id": f"adp_{cid[:8]}_{item_id}", "title": title,
            "url": (f"{_PORTAL}?cid={cid}&ccId={ccid}"
                    f"&jobId={item_id}&lang=en_US"),
            "location": _location_str(req), "description": "",
            "_item_id": item_id}


def _rows(cid, ccid, label, page_size, max_pages):
    """Every requisition on the board, paged; stops (reported) on an error."""
    for page in range(max_pages):
        try:
            r = SESSION.get(
                _API,
                params={"cid": cid, "ccId": ccid, "locale": "en_US",
                        "$top": page_size, "$skip": page * page_size},
                headers=JSON_HEADERS,
            )
            r.raise_for_status()
            reqs = r.json().get("jobRequisitions") or []
        except Exception as e:
            fetch_failed(f"ADP {label} p{page}", e)
            return
        if not reqs:
            return
        for req in reqs:
            yield _row(cid, ccid, req)
        if len(reqs) < page_size:
            return
        time.sleep(0.4)


def fetch_adp(cid, ccid, company_name="", gate=None, loc_re=None, page_size=50,
              max_pages=10, max_details=60, detail_delay=0.2):
    return board_jobs(_rows(cid, ccid, company_name or cid[:8], page_size, max_pages),
                      company_name, gate=gate, loc_re=loc_re,
                      fetch_description=lambda row: _fetch_description(
                          row["_item_id"], cid, ccid),
                      max_details=max_details, detail_delay=detail_delay)
