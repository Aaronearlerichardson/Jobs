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

from src.net.http import JSON_HEADERS, SESSION, fetch_failed, get_json
from src.net.util import text_from_html
from .board import board_jobs


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
    return f"https://{subdomain}.bamboohr.com/careers/{jid}/detail"


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
        r = SESSION.get(f"{base}/careers/list", headers=JSON_HEADERS)
        r.raise_for_status()
        entries = r.json().get("result") or []
    except Exception as e:
        return fetch_failed(f"BambooHR {company_name or subdomain}", e)
    return board_jobs((_row(base, subdomain, e) for e in entries), company_name,
                      gate=gate, loc_re=loc_re,
                      fetch_description=lambda row: _fetch_description(
                          subdomain, row["_jid"], company_name),
                      max_details=max_details, detail_delay=detail_delay)
