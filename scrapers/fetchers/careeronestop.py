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

import re
from urllib.parse import quote


import config

from ..http import SESSION, HEADERS
from ..util import stable_id

# v2 — v1 was retired and returns a blanket 401 even with valid credentials.
_API = "https://api.careeronestop.org/v2/jobsearch"


def _creds():
    uid = (config.CAREERONESTOP_USER_ID or "").strip()
    tok = (config.CAREERONESTOP_TOKEN or "").strip()
    if not uid or not tok:
        print("  [!] CareerOneStop credentials missing.\n"
              "      Register (free): https://www.careeronestop.org/Developers/WebAPI/registration.aspx\n"
              "      then set CAREERONESTOP_USER_ID and CAREERONESTOP_TOKEN env vars.")
        return None
    return uid, tok


_CORP_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "llc", "ltd",
                  "co", "company", "plc", "lp", "the"}


def _tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in _CORP_SUFFIXES}


def _company_match(posted_company, queried_name):
    """Keyword search matches title/description too — keep only rows whose
    Company field is plausibly the employer we asked for. Whole-word token
    match, not substring: 'Meta' must match 'Meta Platforms, Inc.' but NOT
    'Metallurgy Startup LLC' (substring matching failed exactly that way)."""
    p, q = _tokens(posted_company), _tokens(queried_name)
    return bool(p and q) and (q <= p or p <= q)


def _default_location():
    """A location term this API will accept, derived from [locality].

    NOT `[locality].name` verbatim: that is a human label and may be
    anything ("North Carolina / Research Triangle"). A `/` in it silently
    becomes an extra path segment — `quote()` leaves `/` alone by default —
    and the request 404s on a malformed path rather than failing loudly.
    Prefer a spelled-out state/region term, else the longest place name."""
    words = [s for s in config.LOCALITY_STATE_SUFFIX if len(s) > 2]
    if words:
        return max(words, key=len)
    places = [s for s in config.LOCALITY_SUBSTRINGS if s]
    if places:
        return max(places, key=len)
    # Last resort: the label, with anything path-breaking taken off.
    return re.split(r"[/|,]", config.LOCALITY_NAME or "")[0].strip()


def fetch_nlx_company(name, location=None, days=60,
                      page_size=50, max_pages=6):
    """All NLx postings for one employer in `location`. Returns normalized
    job dicts ({id, title, company, url, location, description}) ready for
    ingest_external_jobs; company is canonicalized to `name` so the store's
    company-linking (and the multi-division ranking floor) applies.

    `location` defaults to a term derived from your [locality] — the API
    requires a location segment in the path, so there is no "anywhere" to
    fall back to. It wants a real place ("North Carolina", "Durham, NC"),
    not a label."""
    location = location or _default_location()
    creds = _creds()
    if not creds:
        return []
    if not location:
        print("  [!] No [locality] configured — NLx needs a location "
              "(a state or metro) to search. Set [locality].name.")
        return []
    uid, tok = creds
    hdr = {**HEADERS, "Authorization": f"Bearer {tok}", "Accept": "application/json"}

    out, seen, dropped = [], set(), 0
    for page in range(max_pages):
        # Path: /{userId}/{keyword}/{location}/{radius}/{sortCol}/{sortOrder}
        #       /{startRecord}/{limitRecord}/{days}
        # safe="" so a slash inside a company or location name is encoded
        # rather than punched through as a new path segment (a 404 that
        # looks like "the API is down" but is a malformed URL).
        url = (f"{_API}/{quote(uid, safe='')}/{quote(name, safe='')}/"
               f"{quote(location, safe='')}/25/0/0/"
               f"{page * page_size}/{page_size}/{days}")
        try:
            r = SESSION.get(url, timeout=config.FETCH_TIMEOUT, headers=hdr,
                             params={"showFilters": "false",
                                     "enableJobDescriptionSnippet": "true"})
        except Exception as e:
            print(f"  [!] CareerOneStop request failed: {e}")
            break
        if r.status_code == 401:
            print("  [!] CareerOneStop rejected the credentials (401) — "
                  "check CAREERONESTOP_USER_ID / CAREERONESTOP_TOKEN.")
            break
        if r.status_code != 200:
            print(f"  [!] CareerOneStop HTTP {r.status_code}")
            break
        try:
            data = r.json()
        except ValueError:
            print("  [!] CareerOneStop returned non-JSON")
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
