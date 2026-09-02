"""USAJOBS Search API — the federal government's own job board.

Announcements are read from ``https://data.usajobs.gov/api/search``, whose
``SearchResult.SearchResultItems`` each carry a ``MatchedObjectDescriptor``
holding the title, duty stations, hiring organization, pay band, and the
qualification text. Search scope comes from profile ``[sources.usajobs]``:
``keyword``, ``location``, ``radius``, and the occupational ``series`` codes
that do the real filtering.

Auth is two environment variables read through ``config`` —
``USAJOBS_API_KEY`` and ``USAJOBS_EMAIL`` (the address the key was
registered to, which the API wants as the User-Agent). Both missing means
the source prints one line and returns ``[]``; it never raises.

Notes:
    Why this exists: federal campuses are invisible to every other source
    here. An agency lab (NIEHS and the EPA both sit in Research Triangle
    Park) runs no Greenhouse/Lever/Workday board, files nothing with the
    state job bank, and never reaches an aggregator — its data-science,
    scientific-computing and IT-data openings live only on USAJOBS. One
    free key makes that whole tier of local employers crawlable.

    Register at https://developer.usajobs.gov/apirequest/ — free, and the
    key arrives by email. A key sent with a User-Agent other than the
    registered address is rejected, which is why the email is required
    rather than optional.

    Agencies re-announce one vacancy under several announcement numbers
    (internal vs. public, one per grade), so near-duplicate titles are
    normal rather than a parsing bug.
"""

import html
import re

import config

from core.filters import is_relevant
from ..http import SESSION, HEADERS
from ..util import norm_posted_date

API_URL = "https://data.usajobs.gov/api/search"

# Occupational series crawled when the profile names none: 2210 IT
# management, 1550 computer science, 0601 general health science, 0401
# general biological science.
DEFAULT_SERIES = ("2210", "1550", "0601", "0401")

# The API caps ResultsPerPage at 500. 250 is big enough that a regional
# search is normally one request, small enough to stay polite.
DEFAULT_RESULTS_PER_PAGE = 250

# Hard stop, so a mistyped filter cannot walk the entire federal board.
MAX_PAGES = 20


def _clean(value):
    """Collapse a USAJOBS HTML fragment to single-spaced plain text.

    >>> _clean("<p>Data  Scientist</p>")
    'Data Scientist'
    >>> _clean("R&amp;D lead")
    'R&D lead'

    Anything empty (including a missing key's ``None``) becomes "":

    >>> _clean(None), _clean("")
    ('', '')
    """
    if not value:
        return ""
    text = html.unescape(str(value))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _salary_text(remuneration):
    """The advertised pay band from a ``PositionRemuneration`` list.

    >>> _salary_text([{"MinimumRange": "99908", "MaximumRange": "129878",
    ...                "Description": "Per Year"}])
    '$99,908 - $129,878 Per Year'

    A one-sided or unparseable band never blocks the posting:

    >>> _salary_text([{"MinimumRange": "50", "Description": "Per Hour"}])
    '$50 Per Hour'
    >>> _salary_text([{"MinimumRange": "n/a"}])
    ''
    >>> _salary_text(None)
    ''
    """
    for pay in remuneration or []:
        if not isinstance(pay, dict):
            continue
        interval = _clean(pay.get("Description") or pay.get("RateIntervalCode"))
        try:
            amounts = [f"${float(v):,.0f}"
                       for v in (pay.get("MinimumRange"), pay.get("MaximumRange"))
                       if v not in (None, "")]
        except (TypeError, ValueError):
            continue
        if amounts:
            return " ".join([" - ".join(amounts), interval]).strip()
    return ""


def _locations(descriptor):
    """Every duty station on the announcement, joined with "; ".

    >>> _locations({"PositionLocation": [{"LocationName": "Durham, NC"},
    ...                                  {"LocationName": "Reston, VA"}]})
    'Durham, NC; Reston, VA'

    Repeats collapse, and an empty list falls back to the display field:

    >>> _locations({"PositionLocation": [{"LocationName": "Durham, NC"},
    ...                                  {"LocationName": "Durham, NC"}]})
    'Durham, NC'
    >>> _locations({"PositionLocationDisplay": "Multiple Locations"})
    'Multiple Locations'

    Notes:
        One vacancy is often open at several campuses. Keeping them all is
        what lets the locality gate see the local one instead of whichever
        station the agency happened to list first.
    """
    names = []
    for loc in descriptor.get("PositionLocation") or []:
        if not isinstance(loc, dict):
            continue
        name = _clean(loc.get("LocationName") or loc.get("CityName"))
        if name and name not in names:
            names.append(name)
    return "; ".join(names) or _clean(descriptor.get("PositionLocationDisplay"))


def _describe(details, descriptor):
    """The scoreable body of an announcement: summary, duties, quals, pay.

    ``MajorDuties`` is a list of paragraphs and is flattened in order:

    >>> _describe({"JobSummary": "Lead the lab.",
    ...            "MajorDuties": ["Analyze data.", "Write code."]},
    ...           {"QualificationSummary": "PhD or equivalent."})
    'Lead the lab. Analyze data. Write code. PhD or equivalent.'

    A pay band, when the announcement carries one, is appended last:

    >>> _describe({}, {"QualificationSummary": "BS in a technical field.",
    ...                "PositionRemuneration": [{"MinimumRange": "80000",
    ...                                          "MaximumRange": "90000",
    ...                                          "Description": "Per Year"}]})
    'BS in a technical field. Salary: $80,000 - $90,000 Per Year'

    An announcement with none of those fields yields "":

    >>> _describe({}, {})
    ''
    """
    duties = details.get("MajorDuties")
    if isinstance(duties, (list, tuple)):
        duties = " ".join(_clean(d) for d in duties if d)
    chunks = [_clean(details.get("JobSummary")),
              _clean(duties),
              _clean(descriptor.get("QualificationSummary"))]
    salary = _salary_text(descriptor.get("PositionRemuneration"))
    if salary:
        chunks.append(f"Salary: {salary}")
    return " ".join(c for c in chunks if c).strip()


def _parse_item(item):
    """One ``SearchResultItem`` as a crawler job dict, or None if unusable.

    The id is namespaced by source, and the company is the hiring
    ORGANIZATION rather than the cabinet department:

    >>> job = _parse_item({
    ...     "MatchedObjectId": "830216800",
    ...     "MatchedObjectDescriptor": {
    ...         "PositionTitle": "Data Scientist",
    ...         "PositionURI": "https://www.usajobs.gov/job/830216800",
    ...         "OrganizationName": "Environmental Protection Agency",
    ...         "DepartmentName": "Agency Group",
    ...         "PositionLocation": [{"LocationName": "Durham, NC"}],
    ...         "PublicationStartDate": "2026-08-03T00:00:00.0000",
    ...         "QualificationSummary": "Experience with Python."}})
    >>> job["id"], job["company"], job["location"]
    ('usajobs_830216800', 'Environmental Protection Agency', 'Durham, NC')
    >>> job["posted_at"]
    '2026-08-03'
    >>> job["description"]
    'Agency Group. Experience with Python.'

    A posting with no ``PositionURI`` falls back to the first ``ApplyURI``,
    and the remote hint is stamped only when the announcement says so:

    >>> job = _parse_item({
    ...     "MatchedObjectId": "1",
    ...     "MatchedObjectDescriptor": {
    ...         "PositionTitle": "Health Scientist",
    ...         "ApplyURI": ["https://www.usajobs.gov/job/1/apply"],
    ...         "UserArea": {"Details": {"RemoteIndicator": True}}}})
    >>> job["url"]
    'https://www.usajobs.gov/job/1/apply'
    >>> job["remote_hint"]
    'usajobs:RemoteIndicator'

    Anything without both an id and a title is dropped rather than stored:

    >>> _parse_item({"MatchedObjectDescriptor": {"PositionTitle": "x"}}) is None
    True
    >>> _parse_item({"MatchedObjectId": "2"}) is None
    True
    >>> _parse_item("not a dict") is None
    True

    Notes:
        "NIEHS" is the employer a reader recognizes; "Department of Health
        and Human Services" is not. The parent department still goes into
        the description so it stays searchable.
    """
    if not isinstance(item, dict):
        return None
    descriptor = item.get("MatchedObjectDescriptor")
    if not isinstance(descriptor, dict):
        return None
    jid = item.get("MatchedObjectId") or descriptor.get("PositionID")
    title = _clean(descriptor.get("PositionTitle"))
    if not jid or not title:
        return None

    url = descriptor.get("PositionURI") or ""
    if not url:
        apply_uris = descriptor.get("ApplyURI") or []
        url = apply_uris[0] if apply_uris else ""

    user_area = descriptor.get("UserArea")
    details = user_area.get("Details") if isinstance(user_area, dict) else None
    if not isinstance(details, dict):
        details = {}

    body = _describe(details, descriptor)
    department = _clean(descriptor.get("DepartmentName"))
    if department:
        body = f"{department}. {body}".strip()

    job = {
        "id":          f"usajobs_{jid}",
        "company":     _clean(descriptor.get("OrganizationName")) or "USAJOBS",
        "title":       title,
        "url":         url,
        "location":    _locations(descriptor),
        "description": body,
        "posted_at":   norm_posted_date(descriptor.get("PublicationStartDate")),
    }
    # Stamped only when the announcement really is remote: remote_signal_for()
    # treats the presence of any hint as decisive.
    if details.get("RemoteIndicator") is True:
        job["remote_hint"] = "usajobs:RemoteIndicator"
    return job


def _search_params(keyword, location, radius, series, results_per_page):
    """The query string for one search, minus the page number.

    Series codes go to ``JobCategoryCode`` semicolon-joined, and ``Radius``
    is only meaningful alongside a location:

    >>> params = _search_params("data", "Durham, North Carolina", 25,
    ...                         ["2210", "1550"], 250)
    >>> sorted(params.items())
    [('JobCategoryCode', '2210;1550'), ('Keyword', 'data'), ('LocationName', 'Durham, North Carolina'), ('Radius', 25), ('ResultsPerPage', 250)]

    ``series=None`` means the built-in technical set; an empty list means
    "every series", and unset scope keys are simply omitted:

    >>> _search_params(None, None, 50, None, 250)["JobCategoryCode"]
    '2210;1550;0601;0401'
    >>> sorted(_search_params(None, None, 50, [], 25))
    ['ResultsPerPage']
    """
    codes = [str(s).strip()
             for s in (DEFAULT_SERIES if series is None else series)
             if str(s).strip()]
    params = {"ResultsPerPage": int(results_per_page)}
    if keyword:
        params["Keyword"] = keyword
    if location:
        params["LocationName"] = location
        if radius:
            params["Radius"] = int(radius)
    if codes:
        params["JobCategoryCode"] = ";".join(codes)
    return params


def _credentials():
    """The (key, registered email) pair from config, or None with one line.

    Tested in ``tests/test_fetcher_parsers.py::TestUsajobs`` — it reads the
    live config module, so it needs a monkeypatch rather than a doctest.

    Notes:
        Missing credentials are not an error. The source is opt-in, and a
        crawl with no federal key must still run every other source, so
        this degrades exactly like a dead board.
    """
    key = (getattr(config, "USAJOBS_API_KEY", "") or "").strip()
    email = (getattr(config, "USAJOBS_EMAIL", "") or "").strip()
    if not key or not email:
        print("  [!] USAJOBS skipped: set USAJOBS_API_KEY and USAJOBS_EMAIL "
              "(free key: https://developer.usajobs.gov/apirequest/).")
        return None
    return key, email


def fetch_usajobs(keyword=None, location=None, radius=None, series=None,
                  results_per_page=DEFAULT_RESULTS_PER_PAGE,
                  max_pages=MAX_PAGES):
    """Search USAJOBS and return the relevant announcements as job dicts.

    Pages until ``SearchResult.SearchResultCountAll`` is covered. `series`
    is a list of occupational series codes; every scope argument normally
    comes from profile ``[sources.usajobs]``.

    Returns [] — never raises — with no credentials, on an HTTP or JSON
    error, and on an unexpected payload shape. Covered by
    ``tests/test_fetcher_parsers.py::TestUsajobs``.
    """
    creds = _credentials()
    if not creds:
        return []
    key, email = creds
    headers = {**HEADERS, "Host": "data.usajobs.gov", "User-Agent": email,
               "Authorization-Key": key, "Accept": "application/json"}
    params = _search_params(keyword, location, radius, series, results_per_page)

    jobs, seen, fetched, total = [], set(), 0, None
    for page in range(1, int(max_pages) + 1):
        try:
            r = SESSION.get(API_URL, timeout=25, headers=headers,
                            params={**params, "Page": page})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"    [!] USAJOBS: {e}")
            break
        result = data.get("SearchResult") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            break
        items = result.get("SearchResultItems")
        if not isinstance(items, list) or not items:
            break
        fetched += len(items)
        if total is None:
            try:
                total = int(result.get("SearchResultCountAll") or 0)
            except (TypeError, ValueError):
                total = 0

        for item in items:
            job = _parse_item(item)
            if not job or job["id"] in seen:
                continue
            seen.add(job["id"])
            if is_relevant(job["title"], job["description"]):
                jobs.append(job)

        if not total or fetched >= total:
            break
    return jobs
