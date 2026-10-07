"""NLx feed via the CareerOneStop (US DOL) Web API.

Why this exists: federal contractors — which includes Meta, Google, NVIDIA,
Qualcomm, Microsoft, Amazon — are required under VEVRAA (41 CFR 60-300.5) to
list their US openings with the state job bank where the job sits. Those
listings flow through the National Labor Exchange (NLx) into NCWorks and
DOL's CareerOneStop. So this ONE public API legitimately enumerates NC
postings from employers whose own careers sites are bot-gated (Meta 400,
Google 404, Qualcomm/Eightfold 403 — all verified).

Auth: free key from
https://www.careeronestop.org/Developers/WebAPI/registration.aspx
(DOL emails a UserId + Bearer token). Set:

    $env:CAREERONESTOP_USER_ID = "..."
    $env:CAREERONESTOP_TOKEN   = "..."

Caveats, honestly: NLx compliance feeds lag by days, dedupe imperfectly,
skip executive roles, and search results carry NO job description — so
resume-fit scores for these postings cap at the no-description ceiling
until you open the URL. Better a shallow lead than an invisible job.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

from src import config
from src.net import http
from src.net.http import HEADERS, fetch_failed
from src.net.util import default_search_text, stable_id
from src.rows import FetchedJob


def _tokens(s: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in {"inc", "incorporated", "corp", "corporation", "llc", "ltd",
                          "co", "company", "plc", "lp", "the"}}


def _company_match(posted_company: str | None, queried_name: str) -> bool:
    """Keyword search matches title/description too — keep only rows whose
    Company field is plausibly the employer we asked for. Whole-word token
    match, not substring: 'Meta' must match 'Meta Platforms, Inc.' but NOT
    'Metallurgy Startup LLC' (substring matching failed exactly that way)."""
    p, q = _tokens(posted_company), _tokens(queried_name)
    return bool(p and q) and (q <= p or p <= q)


async def fetch_nlx_company(name: str, location: str | None = None, days: int = 60,
                            page_size: int = 50, max_pages: int = 6) -> list[FetchedJob]:
    """All NLx postings for one employer in `location`. Returns normalized
    job dicts ({id, title, company, url, location, description}) ready for
    ingest_external_jobs; company is canonicalized to `name` so the store's
    company-linking (and the multi-division ranking floor) applies.

    `location` defaults to a term derived from your [locality] — the API
    requires a location segment in the path, so there is no "anywhere" to
    fall back to. It wants a real place ("North Carolina", "Durham, NC"),
    not a label."""
    # Else the [locality] label with `/` cut: a raw `/` in this API's path
    # segment 404s, and `quote()` leaves it alone.
    location = (location or default_search_text()
                or re.split(r"[/|,]", config.LOCALITY_NAME or "")[0].strip())
    # (user id, token), or None with one line out: config.require_creds,
    # which both keyed sources share.
    creds = config.require_creds(
        "CareerOneStop",
        "https://www.careeronestop.org/Developers/WebAPI/registration.aspx",
        CAREERONESTOP_USER_ID=getattr(config, "CAREERONESTOP_USER_ID", ""),
        CAREERONESTOP_TOKEN=getattr(config, "CAREERONESTOP_TOKEN", ""))
    if not creds:
        return []
    if not location:
        print("  [!] No [locality] configured — NLx needs a location "
              "(a state or metro) to search. Set [locality].name.")
        return []
    uid, tok = creds
    hdr = {**HEADERS, "Authorization": f"Bearer {tok}", "Accept": "application/json"}

    out: list[FetchedJob] = []
    seen: set[str | int] = set()
    dropped = 0
    for page in range(max_pages):
        # Path: /{userId}/{keyword}/{location}/{radius}/{sortCol}/{sortOrder}
        #       /{startRecord}/{limitRecord}/{days}
        # safe="" so a slash inside a company or location name is encoded
        # rather than punched through as a new path segment (a 404 that
        # looks like "the API is down" but is a malformed URL). v2: v1 was
        # retired and returns a blanket 401 even with valid credentials.
        url = (f"https://api.careeronestop.org/v2/jobsearch/{quote(uid, safe='')}/"
               f"{quote(name, safe='')}/{quote(location, safe='')}/25/0/0/"
               f"{page * page_size}/{page_size}/{days}")
        try:
            r = await http.send("GET", url, headers=hdr,
                                params={"showFilters": "false",
                                        "enableJobDescriptionSnippet": "true"})
        except Exception as e:
            fetch_failed("CareerOneStop", e, indent=2)
            break
        if r.status_code == 401:
            fetch_failed("CareerOneStop", "rejected the credentials (401)"
                         " - check CAREERONESTOP_USER_ID / "
                         "CAREERONESTOP_TOKEN", indent=2)
            break
        if r.status_code != 200:
            fetch_failed("CareerOneStop", f"HTTP {r.status_code}", indent=2)
            break
        try:
            data = await asyncio.to_thread(r.json)
        except ValueError:
            fetch_failed("CareerOneStop", "non-JSON response", indent=2)
            break
        rows = data.get("Jobs") or []
        if not isinstance(rows, list) or not rows:
            break
        for j in rows:
            if not isinstance(j, dict):
                continue
            company = j.get("Company") or ""
            if not _company_match(company, name):
                dropped += 1
                continue
            jid = j.get("JvId") or stable_id(j.get("URL", ""), j.get("JobTitle", ""))
            if jid in seen:
                continue
            seen.add(jid)
            out.append({
                "id": f"nlx_{jid}",
                "title": (j.get("JobTitle") or "").strip(),
                # Canonical queried name, not the filing's legal name
                # ("Meta Platforms, Inc.") — so store.company_id_by_name links.
                "company": name,
                "url": j.get("URL") or "",
                "location": (j.get("Location") or "").strip(),
                "description": "",   # NLx search results carry none
            })
        try:
            total = int(data.get("Jobcount") or 0)
        except (TypeError, ValueError):
            total = 0
        if (page + 1) * page_size >= total:
            break
    if dropped:
        print(f"    ({dropped} result(s) mentioned {name!r} but were other employers — skipped)")
    return out
