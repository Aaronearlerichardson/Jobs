"""ATS signatures: recognise a hosted applicant-tracking board from a URL or
a page body, and read its coordinates (slug / tenant triple) off the match.

Pure text layer -- regex tables plus `detect` / `pack` / `extract_workday_triple`,
no network and no imports from src/discovery/ or src/crawl/ -- so both the
discovery paths (careers-page sniffer, Workday probes, ATS dorking, web-search
resolution) and the crawl-side consumers (fetchers/getro.py's employer
attribution) can share one definition of "what is a board".
"""

import html
import re

# ─── Platform signatures ─────────────────────────────────────────────────
#
# Fetchable platforms: regex captures the board slug; confirmable via a
# live count (src.discovery.resolve.probes / ADP requisition API). ADP needs two params
# (cid, ccId), handled specially. Workday (a triple) is detected first via
# extract_workday_triple — highest confidence.
ATS_LINK_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("lever",      re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("ashby",      re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)", re.I)),
    ("kula",       re.compile(r"careers\.kula\.ai/([a-z0-9_-]+)", re.I)),
    ("jazzhr",     re.compile(r"([a-z0-9-]+)\.applytojob\.com", re.I)),
    ("bamboohr",   re.compile(r"([a-z0-9-]+)\.bamboohr\.com", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9]+)/", re.I)),
    # Paylocity: the board slug is the 36-char company GUID in the board URL
    # (recruiting.paylocity.com/recruiting/jobs/All/<guid>/<name>). Fetchable
    # via src/ats/fetchers/paylocity.py; the URL's name segment is cosmetic.
    ("paylocity", re.compile(r"recruiting\.paylocity\.com/[Rr]ecruiting/[Jj]obs/All/([0-9a-fA-F-]{36})", re.I)),
    # Rippling: board slug in ats.rippling.com/<slug>/jobs (public JSON API).
    ("rippling", re.compile(r"ats\.rippling\.com/([a-z0-9][a-z0-9-]+)/jobs", re.I)),
    # HiBob: tenant subdomain of careers.hibob.com (public JSON API at
    # <tenant>.careers.hibob.com/api/job-ad — see fetchers/hibob.py).
    ("hibob", re.compile(r"([a-z0-9][a-z0-9-]+)\.careers\.hibob\.com", re.I)),
    # Jobvite: tenant slug of jobs.jobvite.com/<tenant> (server-rendered
    # listing + JSON-LD job pages — see fetchers/jobvite.py).
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([a-z0-9][a-z0-9_-]*)", re.I)),
]
_ADP_CID_RE  = re.compile(r"[?&]cid=([0-9a-f-]{8,})", re.I)
_ADP_CCID_RE = re.compile(r"[?&]ccid=([0-9A-Za-z_]+)", re.I)
_UKG_RE = re.compile(r"recruiting2?\.ultipro\.com/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F\-]{36})", re.I)

# Semi-fetchable: no probe/confirm path, but the local track has best-effort
# scrapers (fetchers/company.py), so sniff_ats surfaces them as coordinates
# while sniff_careers_ats treats them as leads.
SEMI_FETCHABLE_PATTERNS = [
    ("icims",           re.compile(r"([a-z0-9-]+)\.icims\.com", re.I)),
    ("successfactors",  re.compile(r"([a-z0-9-]+)\.(?:successfactors|sapsf)\.(?:com|eu)", re.I)),
]

# PeopleAdmin (most public universities). Handled in detect beside ADP and
# UKG rather than listed above, for the same reason those two are: its store
# identity is not the captured slug. src.store.board_key keys a peopleadmin
# row on careers_url — the tenant serves one campus and the Atom feed hangs
# off the host — so pack rebuilds the origin from the capture, and a
# consumer of the pattern tables that only reads (ats, slug) would mint a
# row with no board identity at all.
_PEOPLEADMIN_RE = re.compile(r"([a-z0-9-]+)\.peopleadmin\.com", re.I)

# Detection-only platforms: real ATSes we can recognize but not reliably
# auto-fetch (bot-protected APIs or JS-only boards). Each regex captures a
# short identifying host/path for the lead note.
ATS_LEAD_PATTERNS = [
    ("eightfold",       re.compile(r"([a-z0-9-]+\.eightfold\.ai)", re.I)),
    ("dayforce",        re.compile(r"(dayforcehcm\.com/[a-zA-Z-]+/[a-zA-Z0-9_-]+)", re.I)),
    ("workable",        re.compile(r"(apply\.workable\.com/[a-z0-9-]+)", re.I)),
    ("recruitee",       re.compile(r"([a-z0-9-]+\.recruitee\.com)", re.I)),
    ("teamtailor",      re.compile(r"([a-z0-9-]+\.teamtailor\.com)", re.I)),
    ("taleo",           re.compile(r"([a-z0-9-]+\.taleo\.net)", re.I)),
    ("ukg",             re.compile(r"([a-z0-9-]+\.ultipro\.com)", re.I)),
    # NOTE: Paylocity moved up to ATS_LINK_PATTERNS (now fetchable via the
    # paylocity fetcher) — it must stay a confirmable path, not a lead.
    ("paycom",          re.compile(r"(paycomonline\.net/[A-Za-z0-9/_-]+)", re.I)),
    ("breezy",          re.compile(r"([a-z0-9-]+\.breezy\.hr)", re.I)),
    ("gohire",          re.compile(r"([a-z0-9-]+\.gohire\.io)", re.I)),
    # NOTE: Workday is intentionally NOT here — it's fetchable via the CXS
    # API (probe_workday confirms with a live count), so it must stay a
    # confirmable path, not a detection-only lead.
]

# The single-capture regexes above match a subdomain or path segment, and
# some of those are structural rather than a board: the vendor's own site
# (www.bamboohr.com, help.applytojob.com), or an embed/asset path of the
# greenhouse URL forms (boards.greenhouse.io/embed/job_board?for=<real slug>,
# boards.greenhouse.io/js). Never a board. One list for every consumer --
# the sniffer's page detection and the dork's URL harvest used to keep
# different halves of it.
BAD_SLUGS = frozenset({
    "www", "help", "support", "blog", "app", "careers", "jobs", "secure",
    "embed", "job_board", "js", "boards", "job-boards", "search", "api",
})

# Fetchable-ATS host detector — used to skip a provided careers_url when
# it's itself a dead slug-guess against a JSON ATS (already covered by
# slug probing upstream).
FETCHABLE_HOST_RE = re.compile(
    r"(greenhouse\.io|lever\.co|ashbyhq\.com|kula\.ai|applytojob\.com|bamboohr\.com|"
    r"careers\.hibob\.com|jobs\.jobvite\.com)",
    re.I,
)

# Every pattern whose capture is a board slug the crawl can fetch.
SIGS = ATS_LINK_PATTERNS + SEMI_FETCHABLE_PATTERNS


# ─── Workday (a tenant + pod + site triple, not a slug) ───────────────────
#
# Workday URLs are a tenant+pod+site triple we can't derive from the
# company name alone (e.g. redhat.wd5.myworkdayjobs.com/Jobs_External).
# Prefer the CXS URL (high confidence: tenant appears twice) and fall
# back to any public board URL. `site` is the segment AFTER any
# optional en-US locale prefix.
_WD_CXS_RE = re.compile(
    r"https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com"
    r"/wday/cxs/[a-z0-9-]+/([A-Za-z0-9_-]+)/",
    re.IGNORECASE,
)
_WD_BOARD_RE = re.compile(
    r"https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com"
    r"(?:/[a-z]{2}-[A-Z]{2})?"
    r"/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
# Segments that show up as the "site" slot but are API paths or assets,
# never real board names.
_WD_SITE_BLOCKLIST = {"wday", "cxs", "api", "static", "assets", "login"}

# A single posting's URL: the site segment, then everything from /job/ on,
# which is what the CXS detail endpoint wants as its path. Same optional
# locale prefix as _WD_BOARD_RE -- triage carried its own copy of this that
# only knew /en-US and /en, so a board served under any other locale fell
# back to scraping the rendered page.
_WD_JOB_PATH_RE = re.compile(
    r"myworkdayjobs\.com(?:/[a-z]{2}(?:-[A-Za-z]{2})?)?/([^/]+)(/job/.*)$",
    re.IGNORECASE,
)


def workday_job_path(url):
    """(site, path) from a Workday JOB url, or None if it isn't one.

    >>> workday_job_path("https://acme.wd5.myworkdayjobs.com/External/job/RTP/Engineer_R1")
    ('External', '/job/RTP/Engineer_R1')
    >>> workday_job_path("https://acme.wd5.myworkdayjobs.com/en-US/External/job/x")
    ('External', '/job/x')

    A board URL with no posting on it has no path to give:

    >>> workday_job_path("https://acme.wd5.myworkdayjobs.com/en-US/External") is None
    True
    >>> workday_job_path("") is None
    True
    """
    m = _WD_JOB_PATH_RE.search(url or "")
    return (m.group(1), m.group(2)) if m else None


def extract_workday_triple(text):
    """(tenant, wd_pod_int, site) from the first Workday URL found in
    `text`, or None. Checks the CXS API form first (higher signal), then
    falls back to any public board URL.

    >>> extract_workday_triple("https://acme.wd5.myworkdayjobs.com/en-US/External/job/x")
    ('acme', 5, 'External')
    >>> extract_workday_triple("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Jobs/jobs")
    ('acme', 5, 'Jobs')

    An API/asset segment in the site slot is not a board:

    >>> extract_workday_triple("https://acme.wd5.myworkdayjobs.com/wday/authgwy") is None
    True
    >>> extract_workday_triple("") is None
    True
    """
    if not text:
        return None
    m = _WD_CXS_RE.search(text)
    if m:
        return m.group(1).lower(), int(m.group(2)), m.group(3)
    for tenant, pod, site in _WD_BOARD_RE.findall(text):
        if site.lower() not in _WD_SITE_BLOCKLIST:
            return tenant.lower(), int(pod), site
    return None


# ─── Detection ───────────────────────────────────────────────────────────

def detect(text, final_url=""):
    """Scan text + final URL for an ATS signature.

    Returns (kind, ats, slug) where kind is "fetchable" | "semi" | "lead",
    or None. Workday first (triple, highest confidence), then ADP (two
    params, generic host), UKG/UltiPro and PeopleAdmin (host-shaped), then
    the single-capture platforms in table order.

    >>> detect("", "https://boards.greenhouse.io/acmebio/jobs/1")
    ('fetchable', 'greenhouse', 'acmebio')
    >>> detect("", "https://acme.wd5.myworkdayjobs.com/en-US/External")
    ('fetchable', 'workday', ('acme', 5, 'External'))
    >>> detect("<a href='https://acme.icims.com/jobs'>Jobs</a>")
    ('semi', 'icims', 'acme')
    >>> detect("via acme.eightfold.ai portal")
    ('lead', 'eightfold', 'acme.eightfold.ai')

    A vendor's own site or an embed path is not a board (BAD_SLUGS):

    >>> detect("", "https://www.bamboohr.com/") is None
    True
    >>> detect("", "https://boards.greenhouse.io/embed/job_board/js?for=acme") is None
    True
    >>> detect("", "https://example.com/careers") is None
    True
    """
    blob = f"{final_url}\n{text}"
    triple = extract_workday_triple(blob)
    if triple:
        return "fetchable", "workday", triple
    if "workforcenow.adp.com" in blob.lower():
        unescaped = html.unescape(blob)
        cid = _ADP_CID_RE.search(unescaped)
        ccid = _ADP_CCID_RE.search(unescaped)
        if cid and ccid:
            return "fetchable", "adp", f"{cid.group(1)}|{ccid.group(1)}"
    # UKG Pro (UltiPro): slug is CODE|GUID from the board URL.
    ukg = _UKG_RE.search(blob)
    if ukg:
        return "fetchable", "ultipro", f"{ukg.group(1)}|{ukg.group(2)}"
    # PeopleAdmin: only the HOSTED tenants carry a signature. A university
    # serving the same software from its own hostname (jobs.ncsu.edu) is
    # indistinguishable from any other careers page here and still has to be
    # registered by hand — see src.discovery.local_sourcing.add_board.
    pa = _PEOPLEADMIN_RE.search(blob)
    if pa and pa.group(1).lower() not in BAD_SLUGS:
        return "semi", "peopleadmin", pa.group(1)
    for kind, patterns in (("fetchable", ATS_LINK_PATTERNS),
                           ("semi", SEMI_FETCHABLE_PATTERNS),
                           ("lead", ATS_LEAD_PATTERNS)):
        for ats, rx in patterns:
            m = rx.search(blob)
            if not m:
                continue
            slug = m.group(1)
            if kind != "lead" and slug.lower() in BAD_SLUGS:
                continue
            if slug and len(slug) >= 2:
                return kind, ats, slug
    return None


def pack(ats, slug, careers_url):
    """A detection -> the coordinate dict every resolver consumes.

    Workday's coordinates are a (tenant, pod, site) triple, so they travel
    under `triple`; every other platform has one slug:

    >>> pack("greenhouse", "acme", "https://acme.com/careers")["slug"]
    'acme'
    >>> pack("workday", ("acme", 5, "External"), "")["triple"]
    ('acme', 5, 'External')

    PeopleAdmin rows are keyed on `careers_url` rather than on the slug
    (src.store.board_key), so a hosted tenant's URL is reduced to its
    origin — whichever page of the tenant carried the signature, the board
    comes out the same:

    >>> pack("peopleadmin", "unc",
    ...      "https://unc.peopleadmin.com/postings/search?x=1")["careers_url"]
    'https://unc.peopleadmin.com'

    Nothing else is rewritten, including a PeopleAdmin tenant on its own
    hostname, which never matches the signature and reaches the store by
    hand instead:

    >>> pack("custom", None, "https://jobs.ncsu.edu/")["careers_url"]
    'https://jobs.ncsu.edu/'
    """
    if ats == "peopleadmin" and slug:
        careers_url = f"https://{slug}.peopleadmin.com"
    out = {"ats": ats, "careers_url": careers_url}
    if ats == "workday":
        out["triple"] = slug
    else:
        out["slug"] = slug
    return out
