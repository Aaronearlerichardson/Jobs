"""ADP Workforce Now public job-requisitions fetcher.

ADP WFN career centers embed at
``workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html``
with two identifying query params: ``cid`` (a GUID) and ``ccId``. The
same params drive an unauthenticated JSON API:

    /mascsr/default/careercenter/public/events/staffing/v1/
        job-requisitions?cid=<cid>&ccId=<ccid>&locale=en_US&$top=N&$skip=K

Each requisition has itemID, requisitionTitle, postDate, and
requisitionLocations[].nameCode.shortName. There's no per-job description
in the list payload; a detail endpoint exists at
``job-requisitions/<itemID>?cid=...`` and is fetched best-effort.

cid/ccid can't be guessed from a company name — they come from the
discovery sniffer reading the company's careers page (Synchron et al.).
"""

import time

import requests
from bs4 import BeautifulSoup

from ..filters import is_relevant
from ..http import HEADERS

_API = ("https://workforcenow.adp.com/mascsr/default/careercenter/public"
        "/events/staffing/v1/job-requisitions")
_PORTAL = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment"
           "/recruitment.html")
_JSON_HEADERS = {**HEADERS, "Accept": "application/json"}


def _location_str(req):
    locs = req.get("requisitionLocations") or []
    names = []
    for l in locs:
        n = ((l.get("nameCode") or {}).get("shortName") or "").strip()
        if n:
            names.append(n)
    return "; ".join(names) or "Unknown"


def _fetch_description(item_id, cid, ccid, timeout=15):
    try:
        r = requests.get(
            f"{_API}/{item_id}",
            params={"cid": cid, "ccId": ccid, "locale": "en_US"},
            timeout=timeout, headers=_JSON_HEADERS,
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


def fetch_adp(cid, ccid, company_name, page_size=50, max_pages=10,
              max_details=60, detail_delay=0.2):
    jobs, details_fetched = [], 0
    for page in range(max_pages):
        try:
            r = requests.get(
                _API,
                params={"cid": cid, "ccId": ccid, "locale": "en_US",
                        "$top": page_size, "$skip": page * page_size},
                timeout=25, headers=_JSON_HEADERS,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"    [!] ADP {company_name} p{page}: {e}")
            break

        reqs = data.get("jobRequisitions") or []
        if not reqs:
            break

        for req in reqs:
            item_id = str(req.get("itemID") or "")
            title   = req.get("requisitionTitle") or ""
            if not item_id or not title:
                continue

            desc = ""
            if not is_relevant(title) and details_fetched < max_details:
                desc = _fetch_description(item_id, cid, ccid)
                details_fetched += 1
                time.sleep(detail_delay)
            if not is_relevant(title, desc):
                continue
            if not desc and details_fetched < max_details:
                desc = _fetch_description(item_id, cid, ccid)
                details_fetched += 1
                time.sleep(detail_delay)

            jobs.append({
                "id":          f"adp_{cid[:8]}_{item_id}",
                "company":     company_name,
                "title":       title,
                "url":         (f"{_PORTAL}?cid={cid}&ccId={ccid}"
                                f"&jobId={item_id}&lang=en_US"),
                "location":    _location_str(req),
                "description": desc,
            })

        if len(reqs) < page_size:
            break
        time.sleep(0.4)
    return jobs
