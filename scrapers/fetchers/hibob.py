"""HiBob (bob) public careers-site fetcher.

HiBob-hosted boards live at ``<tenant>.careers.hibob.com`` — a client-
rendered SPA (the static HTML is just a `<careers-app-root>` shell), but the
listing comes from a clean public JSON API:

    GET  <tenant>.careers.hibob.com/api/job-ad  -> {filterGroups, jobAdDetails[]}

Each entry carries id / title / department / site / country / workspaceType
(remote|hybrid|on_site) and an HTML description. The endpoint 401s without a
same-origin ``Referer`` — it isn't checking a session, just that header — so
every request sends one pointed at the tenant's own careers root.

There is no per-job deep link: the board is a single-page app with an
in-memory detail panel (clicking a card never changes ``page.url``), so
every job's ``url`` points at the shared ``/jobs`` listing page.

The store slug is the tenant subdomain (e.g. ``liquidia`` for
``liquidia.careers.hibob.com``, discovered via Liquidia Technologies, a
Morrisville NC pharma company whose careers page links out to HiBob with no
other detectable ATS signature on the page itself).
"""

from bs4 import BeautifulSoup

from core.filters import is_relevant
from config import FETCH_TIMEOUT
from ..http import SESSION, HEADERS

_API = "https://{tenant}.careers.hibob.com/api/job-ad"


def parse_board(tenant, timeout=FETCH_TIMEOUT):
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


def fetch_hibob(tenant, company_name):
    """Keyword-gated fetch (for sweeping unvetted boards): the description
    is already inline in the listing payload (no separate detail request,
    unlike Rippling/Paylocity), so there's nothing to lazily hydrate."""
    try:
        raw = parse_board(tenant)
    except Exception as e:
        print(f"    [!] HiBob {company_name}: {e}")
        return []
    out = []
    for j in raw:
        jid = str(j.get("id") or "")
        title = (j.get("title") or "").strip()
        if not jid or not title:
            continue
        desc = BeautifulSoup(j.get("description") or "", "html.parser").get_text(" ", strip=True)
        if not is_relevant(f"{title} {_dept(j)}", desc):
            continue
        rec = {"id": f"hibob_{tenant}_{jid[:12]}", "company": company_name,
               "title": title,
               "url": f"https://{tenant}.careers.hibob.com/jobs",
               "location": location_str(j), "description": desc,
               "posted_at": j.get("publishedAt")}
        if str(j.get("workspaceType", "")).lower() == "remote":
            rec["remote_hint"] = "hibob:workspaceType"
        out.append(rec)
    return out
