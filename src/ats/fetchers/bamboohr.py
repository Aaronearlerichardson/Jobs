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

from src.net.http import JSON_HEADERS, SESSION, get_json
from src.net.util import text_from_html
from .board import board_fetch


def board_url(subdomain):
    """The board root every other URL here hangs off; `detail_url`'s
    doctest pins the host shape through it."""
    return f"https://{subdomain}.bamboohr.com"


def parse_board(subdomain, timeout=None):
    """The raw ``result`` list (the whole board) for one subdomain. Raises
    on any HTTP or JSON failure; `fetch_bamboohr` reports it."""
    r = SESSION.get(f"{board_url(subdomain)}/careers/list",
                    timeout=timeout, headers=JSON_HEADERS)
    r.raise_for_status()
    return r.json().get("result") or []


def _location_str(job):
    loc = job.get("location") or {}
    parts = [loc.get("city"), loc.get("state")]
    joined = ", ".join(p for p in parts if p)
    if _is_remote(job):
        return f"Remote{' / ' + joined if joined else ''}"
    return joined or "Unknown"


def _is_remote(job):
    return bool(job.get("isRemote")) or str(job.get("locationType")) == "1"


def detail_url(subdomain, jid):
    """One posting's JSON detail endpoint.

    >>> detail_url("acme", 29)
    'https://acme.bamboohr.com/careers/29/detail'

    Notes:
        Also what the per-posting closure probe asks
        (fetchers/probe.py): this endpoint 404s once a posting is pulled
        while the posting's own page keeps answering 200, so the two must
        address it the same way.
    """
    return f"{board_url(subdomain)}/careers/{jid}/detail"


def _fetch_description(subdomain, jid, label="", timeout=None):
    """One posting's JD as text, "" when the detail call fails.

    Through net.http.get_json: a detail endpoint that fails is reported
    and counted, not silently indistinguishable from a body-less posting
    (see adp_wfn._fetch_description).
    """
    data = get_json(detail_url(subdomain, jid),
                    f"BambooHR {label or subdomain} job {jid}",
                    timeout=timeout, headers=JSON_HEADERS)
    if not isinstance(data, dict):
        return ""
    opening = (data.get("result") or {}).get("jobOpening") or {}
    return text_from_html(opening.get("description") or "")


def _row(subdomain, entry):
    jid = str(entry.get("id") or "")
    title = entry.get("jobOpeningName") or ""
    if not jid or not title:
        return None
    row = {"id": f"bamboo_{subdomain}_{jid}", "title": title,
           "url": f"{board_url(subdomain)}/careers/{jid}",
           "location": _location_str(entry),
           "description": "",
           "head": f"{title} {entry.get('departmentLabel') or ''}",
           "_jid": jid}
    if _is_remote(entry):
        row["remote_hint"] = "bamboohr:locationType"
    return row


def fetch_bamboohr(subdomain, company_name="", gate=None, loc_re=None,
                   max_details=40, detail_delay=0.2):
    return board_fetch(f"BambooHR {company_name or subdomain}",
                       lambda: parse_board(subdomain),
                       lambda e: _row(subdomain, e),
                       company_name, gate=gate, loc_re=loc_re,
                       fetch_description=lambda row: _fetch_description(
                           subdomain, row["_jid"], company_name),
                       max_details=max_details, detail_delay=detail_delay)
