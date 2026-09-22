"""UKG Pro (UltiPro) recruiting job-board fetcher.

UKG Pro boards live at ``recruiting2.ultipro.com/<CODE>/JobBoard/<GUID>/`` or
``recruiting.ultipro.com/...`` (a client-rendered SPA) and expose a public JSON
search API:

    POST recruiting2.ultipro.com/<CODE>/JobBoard/<GUID>/JobBoardView/LoadSearchResults
      body {"opportunitySearch": {"Top", "Skip", "QueryString", "OrderBy": [], "Filters": []}}
      -> {"opportunities": [{Id, Title, Locations, BriefDescription, ...}], "totalCount"}

The listing carries the ``BriefDescription`` inline, so no per-job detail call
is needed. The store slug is ``"<CODE>|<GUID>"``.

Notes:
    parse_board posts through a bare requests.Session rather than the shared
    PoliteSession (as it has since the fetcher was added), so it names its
    timeout explicitly instead of inheriting the session default.

    The slug does not record which host serves the board (the detector in
    src.ats.signatures accepts both), and a board answers only on its own:
    the other returns 404. parse_board tries ``recruiting2`` first, falls
    back to ``recruiting`` on a 404, and remembers the winner per slug for
    the rest of the process so job URLs are built on the host that worked.
"""

import time

import requests

from src.net.http import DEFAULT_TIMEOUT, JSON_HEADERS
from src.net.util import text_from_html
from .board import board_fetch

_JSON = {**JSON_HEADERS, "Content-Type": "application/json"}


#: Subdomains a board may be served from, in the order they are tried.
_HOSTS = ("recruiting2", "recruiting")

#: slug -> the subdomain that last answered it (learned by parse_board).
_HOST_OF = {}


def _base(slug, host=None):
    """The board's root URL on `host`, default the one known to serve it."""
    code, _, guid = slug.partition("|")
    return f"https://{host or _HOST_OF.get(slug, _HOSTS[0])}.ultipro.com/{code}/JobBoard/{guid}"


def parse_board(slug, page_size=100, max_pages=10, timeout=DEFAULT_TIMEOUT):
    """Return the raw opportunity list for one board slug (``CODE|GUID``)."""
    out = []
    # The host that answered last time goes first; a 404 falls through to the other.
    hosts = sorted(_HOSTS, key=lambda h: h != _HOST_OF.get(slug))
    with requests.Session() as s:
        for page in range(max_pages):
            body = {"opportunitySearch": {"Top": page_size, "Skip": page * page_size,
                                          "QueryString": "", "OrderBy": [], "Filters": []}}
            for host in hosts:
                r = s.post(f"{_base(slug, host)}/JobBoardView/LoadSearchResults",
                           json=body, timeout=timeout, headers=_JSON)
                if r.status_code != 404:
                    break
            r.raise_for_status()
            _HOST_OF[slug] = host
            hosts = [host]      # later pages stay on the host that answered
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
    return text_from_html(opp.get("BriefDescription") or "")


def _detail_url(slug, oid):
    return f"{_base(slug)}/OpportunityDetail?opportunityId={oid}"


def _row(slug, code, o):
    title = (o.get("Title") or "").strip()
    oid = o.get("Id") or ""
    if not title or not oid:
        return None
    return {"id": f"ultipro_{code}_{oid[:12]}", "title": title,
            "url": _detail_url(slug, oid), "location": location_str(o),
            "description": _desc(o)}


def fetch_ultipro(slug, company_name="", gate=None, loc_re=None):
    # No fetch_description: BriefDescription rides the listing (module doc).
    code = slug.split("|")[0]
    return board_fetch(f"UltiPro {company_name or code}",
                       lambda: parse_board(slug), lambda o: _row(slug, code, o),
                       company_name, gate=gate, loc_re=loc_re)
