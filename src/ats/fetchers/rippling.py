"""Rippling ATS public board fetcher.

Rippling-hosted boards live at ``ats.rippling.com/<slug>/jobs`` (a client-
rendered SPA), but the data comes from a clean public JSON API:

    GET  api.rippling.com/platform/api/ats/v1/board/<slug>/jobs         -> listing
    GET  api.rippling.com/platform/api/ats/v1/board/<slug>/jobs/<uuid>  -> one job

The listing carries uuid / name / department / workLocation; the per-job
endpoint adds the full description (an HTML ``{company, role}`` dict) and a
``companyName`` for attribution. The store slug is the board slug (e.g.
``blackrockneurotech``). Filters and the detail budget: fetchers/board.py.

Replaces the old ``custom`` treatment of Rippling boards, whose static HTML
scrape returned nothing because the board is client-rendered.
"""

from src.net.http import JSON_HEADERS, SESSION, get_json
from src.net.util import text_from_html
from .board import board_fetch

_API = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"


def parse_board(slug, timeout=None):
    """Return the raw listing (list of job dicts) for one board slug."""
    r = SESSION.get(_API.format(slug=slug), timeout=timeout, headers=JSON_HEADERS)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else (data.get("jobs") or [])


def location_str(job):
    wl = job.get("workLocation")
    if isinstance(wl, dict) and wl.get("label"):
        return wl["label"]
    wls = job.get("workLocations")
    if isinstance(wls, list) and wls:
        return ", ".join(str(x) for x in wls[:3])
    return "Unknown"


def fetch_description(slug, uuid, label="", timeout=None):
    """Full JD text for one posting. Rippling's description is a
    ``{company, role}`` HTML dict — 'role' is the actual JD (put first);
    'company' is the shared boilerplate.

    Through net.http.get_json: a detail endpoint that fails is reported
    and counted, not silently indistinguishable from a body-less posting
    (see adp_wfn._fetch_description)."""
    data = get_json(f"{_API.format(slug=slug)}/{uuid}",
                    f"Rippling {label or slug} job {uuid[:12]}",
                    timeout=timeout, headers=JSON_HEADERS)
    if not isinstance(data, dict):
        return ""
    d = data.get("description")
    if isinstance(d, dict):
        parts = [d.get("role"), d.get("company")]
    else:
        parts = [d]
    return text_from_html(" ".join(p for p in parts if isinstance(p, str) and p))


def _dept(job):
    d = job.get("department") or {}
    return d.get("label", "") if isinstance(d, dict) else str(d)


def _row(slug, j):
    uuid = j.get("uuid") or ""
    title = (j.get("name") or "").strip()
    if not uuid or not title:
        return None
    return {"id": f"rippling_{slug}_{uuid[:12]}", "title": title,
            "url": j.get("url") or f"https://ats.rippling.com/{slug}/jobs/{uuid}",
            "location": location_str(j), "description": "",
            "head": f"{title} {_dept(j)}", "_uuid": uuid}


def fetch_rippling(slug, company_name="", gate=None, loc_re=None, max_details=40,
                   detail_delay=0.2):
    return board_fetch(f"Rippling {company_name or slug}",
                       lambda: parse_board(slug), lambda j: _row(slug, j),
                       company_name, gate=gate, loc_re=loc_re,
                       fetch_description=lambda row: fetch_description(
                           slug, row["_uuid"], company_name),
                       max_details=max_details, detail_delay=detail_delay)
