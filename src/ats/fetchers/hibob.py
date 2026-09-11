"""HiBob (bob) public careers-site fetcher.

HiBob-hosted boards live at ``<tenant>.careers.hibob.com`` — a client-
rendered SPA (the static HTML is just a `<careers-app-root>` shell), but the
listing comes from a clean public JSON API:

    GET  <tenant>.careers.hibob.com/api/job-ad  -> {filterGroups, jobAdDetails[]}

Each entry carries id / title / department / site / country / workspaceType
(remote|hybrid|on_site) and an HTML description — inline, so unlike
Rippling or Paylocity there is no per-job detail call. The endpoint 401s
without a same-origin ``Referer`` — it isn't checking a session, just that
header — so every request sends one pointed at the tenant's own careers
root.

There is no per-job deep link: the board is a single-page app with an
in-memory detail panel (clicking a card never changes ``page.url``), so
every job's ``url`` points at the shared ``/jobs`` listing page.

The store slug is the tenant subdomain (e.g. ``liquidia`` for
``liquidia.careers.hibob.com``, discovered via Liquidia Technologies, a
Morrisville NC pharma company whose careers page links out to HiBob with no
other detectable ATS signature on the page itself).
"""

from bs4 import BeautifulSoup

from src.net.http import HEADERS, SESSION, fetch_failed
from src.net.util import norm_posted_date
from .board import board_jobs

_API = "https://{tenant}.careers.hibob.com/api/job-ad"


def parse_board(tenant, timeout=None):
    """Return the raw ``jobAdDetails`` list for one tenant subdomain."""
    root = f"https://{tenant}.careers.hibob.com/"
    r = SESSION.get(_API.format(tenant=tenant), timeout=timeout,
                     headers={**HEADERS, "Accept": "application/json", "Referer": root})
    r.raise_for_status()
    data = r.json()
    return data.get("jobAdDetails", []) or [] if isinstance(data, dict) else []


def location_str(job):
    site = job.get("site") or job.get("country") or ""
    workspace = job.get("workspaceType") or ""
    parts = [p for p in (site, workspace) if p]
    return " - ".join(parts) if parts else "Unknown"


def _dept(job):
    return job.get("department") or ""


def _row(tenant, j):
    jid = str(j.get("id") or "")
    title = (j.get("title") or "").strip()
    if not jid or not title:
        return None
    row = {"id": f"hibob_{tenant}_{jid[:12]}", "title": title,
           "url": f"https://{tenant}.careers.hibob.com/jobs",
           "location": location_str(j),
           "description": BeautifulSoup(j.get("description") or "",
                                        "html.parser").get_text(" ", strip=True),
           "posted_at": norm_posted_date(j.get("publishedAt")),
           "head": f"{title} {_dept(j)}"}
    if str(j.get("workspaceType", "")).lower() == "remote":
        row["remote_hint"] = "hibob:workspaceType"
    return row


def fetch_hibob(tenant, company_name="", gate=None, loc_re=None):
    try:
        raw = parse_board(tenant)
    except Exception as e:
        return fetch_failed(f"HiBob {company_name or tenant}", e)
    return board_jobs((_row(tenant, j) for j in raw), company_name,
                      gate=gate, loc_re=loc_re)
