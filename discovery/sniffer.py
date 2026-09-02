"""
Careers-page ATS sniffer — the single implementation shared by every
discovery path (Claude-driven discovery, BCIWiki sweeps, NC local sourcing,
ATS dorking).

Instead of guessing an ATS board slug from a company name (low recall, false
collisions), fetch the company's likely careers page(s) and detect which ATS
is embedded, extracting the *exact* slug/tenant/GUID from the embed link.

This merges the two sniffers built independently on the remote-neural and
"""

import html
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup, SoupStrainer

# File-only diagnostics (session log DEBUG channel — never printed).
_log = logging.getLogger("discovery.sniffer")

_ANCHORS_ONLY = SoupStrainer("a")

from scrapers.http import HEADERS, SESSION
from .probes import PROBES, _extract_workday_triple
from .names import domain_tokens, risky_domain_tokens

# ─── Platform signatures ─────────────────────────────────────────────────
#
# Fetchable platforms: regex captures the board slug; confirmable via a
# live count (slug probes / ADP requisition API). ADP needs two params
# (cid, ccId), handled specially. Workday (a triple) is detected first via
# _extract_workday_triple — highest confidence.
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
    # via scrapers/fetchers/paylocity.py; the URL's name segment is cosmetic.
    ("paylocity", re.compile(r"recruiting\.paylocity\.com/[Rr]ecruiting/[Jj]obs/All/([0-9a-fA-F-]{36})", re.I)),
    # Rippling: board slug in ats.rippling.com/<slug>/jobs (public JSON API).
    ("rippling", re.compile(r"ats\.rippling\.com/([a-z0-9][a-z0-9-]+)/jobs", re.I)),
    # HiBob: tenant subdomain of careers.hibob.com (public JSON API at
    # <tenant>.careers.hibob.com/api/job-ad — see fetchers/hibob.py).
    ("hibob", re.compile(r"([a-z0-9][a-z0-9-]+)\.careers\.hibob\.com", re.I)),
]
_ADP_CID_RE  = re.compile(r"[?&]cid=([0-9a-f-]{8,})", re.I)
_ADP_CCID_RE = re.compile(r"[?&]ccid=([0-9A-Za-z_]+)", re.I)

# Semi-fetchable: no probe/confirm path, but the local track has best-effort
# scrapers (fetchers/company.py), so sniff_ats surfaces them as coordinates
# while sniff_careers_ats treats them as leads.
SEMI_FETCHABLE_PATTERNS = [
    ("icims",           re.compile(r"([a-z0-9-]+)\.icims\.com", re.I)),
    ("successfactors",  re.compile(r"([a-z0-9-]+)\.(?:successfactors|sapsf)\.(?:com|eu)", re.I)),
]

# PeopleAdmin (most public universities). Handled in _detect beside ADP and
# UKG rather than listed above, for the same reason those two are: its store
# identity is not the captured slug. core.store.board_key keys a peopleadmin
# row on careers_url — the tenant serves one campus and the Atom feed hangs
# off the host — so _pack rebuilds the origin from the capture, and a
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
    ("jobvite",         re.compile(r"(jobs\.jobvite\.com/[a-z0-9-]+)", re.I)),
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

_BAD_SUBDOMAINS = ("www", "help", "support", "blog", "app", "careers", "jobs", "secure")

# Fetchable-ATS host detector — used to skip a provided careers_url when
# it's itself a dead slug-guess against a JSON ATS (already covered by
# slug probing upstream).
_FETCHABLE_HOST_RE = re.compile(
    r"(greenhouse\.io|lever\.co|ashbyhq\.com|kula\.ai|applytojob\.com|bamboohr\.com|"
    r"careers\.hibob\.com)",
    re.I,
)

def _looks_like_custom_board(html_text):
    """True if a page has several GENUINE job-detail links (nav/index links
    filtered out) — i.e. a self-hosted careers board worth scraping."""
    from scrapers.fetchers.company import find_job_links
    try:
        soup = BeautifulSoup(html_text, "lxml", parse_only=_ANCHORS_ONLY)
    except Exception:
        return False
    return len(find_job_links(soup)) >= 3


# ─── Candidate careers-page URLs ─────────────────────────────────────────
#
# (host, path) patterns in priority order, applied breadth-first over the
# name's domain tokens (names.domain_tokens) so every token's best guess is
# tried before any token's worst -- including the non-.com TLDs common for
# neurotech / deep-tech startups. The root ("/") sits second: a company
# whose ATS badge is on the homepage has no dedicated /careers page, and a
# Workday-hosted careers site redirects straight from it.
_URL_PATTERNS = [
    ("www.{tok}.com", "/careers"),
    ("www.{tok}.com", "/"),
    ("www.{tok}.com", "/jobs"),
    ("careers.{tok}.com", "/"),
    ("www.{tok}.com", "/en/jobs"),
    ("{tok}.io", "/careers"),
    ("{tok}.ai", "/careers"),
    ("{tok}.bio", "/careers"),
    ("{tok}.xyz", "/careers"),
    ("{tok}.health", "/careers"),
    ("{tok}.co", "/careers"),
    ("jobs.{tok}.com", "/"),
]
ROOT_PATTERNS = [p for p in _URL_PATTERNS if p == ("www.{tok}.com", "/")]

# Cap on speculative GETs per name. A miss pays every one of them.
_URL_CAP = 12


def candidate_urls(name, careers_url="", patterns=_URL_PATTERNS, cap=_URL_CAP):
    """Careers-page URLs to fetch for `name`, best first, `cap` at most.

    >>> for u in candidate_urls("Merakris Therapeutics")[:4]:
    ...     print(u)
    https://www.merakristherapeutics.com/careers
    https://www.merakris.com/careers
    https://www.merakristherapeutics.com/
    https://www.merakris.com/

    A recorded careers_url (e.g. capture.py's JSON-LD hint) is a far better
    base than a name-guess, so it goes first and its host's other paths
    come next -- oxb.com / united-imaging.com resolve even though the name
    never would:

    >>> for u in candidate_urls("Acme", "https://acme.io/careers")[:5]:
    ...     print(u)
    https://acme.io/careers
    https://acme.io/
    https://acme.io/jobs
    https://acme.io/en/jobs
    https://www.acme.com/careers

    ...unless it is itself a dead slug-guess against a JSON ATS, already
    covered by the slug probes upstream:

    >>> candidate_urls("Acme", "https://boards.greenhouse.io/acme")[0]
    'https://www.acme.com/careers'

    `patterns` narrows the list; ROOT_PATTERNS is the bare-homepage subset
    the failure path scans (see _scan_root):

    >>> candidate_urls("Merakris Therapeutics", patterns=ROOT_PATTERNS)
    ['https://www.merakristherapeutics.com/', 'https://www.merakris.com/']
    >>> candidate_urls("Acme", "https://acme.io/careers", patterns=ROOT_PATTERNS)[0]
    'https://acme.io/'

    The list is deduped and capped, and a name with no domain tokens and
    no hint has nothing to try:

    >>> len(candidate_urls("A Very Long Multi Word Company Name Ltd")) <= 12
    True
    >>> candidate_urls("")
    []
    """
    urls = []
    if careers_url and not _FETCHABLE_HOST_RE.search(careers_url):
        if patterns is _URL_PATTERNS:
            urls.append(careers_url)
        base_m = re.match(r"(https?://[^/]+)", careers_url)
        if base_m:
            for path in dict.fromkeys(p for _, p in patterns):
                urls.append(base_m.group(1) + path)
    toks = domain_tokens(name)
    for host, path in patterns:
        for tok in toks:
            urls.append(f"https://{host.format(tok=tok)}{path}")
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
        if cap and len(out) >= cap:
            break
    return out


def _scan_root(name, careers_url=""):
    """Fetch the bare homepage(s) (candidate_urls with ROOT_PATTERNS) and
    return the first fetchable/semi-fetchable ATS hit (packed like
    sniff_ats), else None.

    The root is a regular candidate too, but a multi-token name or a
    careers_url hint can push it past the cap, so the failure path scans it
    separately (the per-run memo makes an already-fetched root free). A hit
    reached only through a risky (truncated/generic) domain token must still
    corroborate the company name on the page -- the same rule candidate hits
    are held to -- so scanning the root doesn't hand the galaxy.com
    collision a second way in (network path, so covered by
    tests/test_parsers.py::TestRootScan rather than a doctest here).
    """
    urls = candidate_urls(name, careers_url, patterns=ROOT_PATTERNS, cap=None)
    if not urls:
        return None
    responses = _fetch_all(urls)
    for url in urls:
        r = responses.get(url)
        if r is None:
            continue
        risky_tok = _risky_token_in_url(url, name)
        if risky_tok and not _corroborates(r.text, name, risky_tok):
            continue
        hit = _detect(r.text, r.url)
        if hit and hit[0] in ("fetchable", "semi"):
            if hit[1] == "workday" and _foreign_board(name, hit[2]):
                continue
            return _pack(hit[1], hit[2], r.url)
    return None


# ─── Truncated-domain corroboration ───────────────────────────────────────
#
# candidate_urls tries every domain TOKEN (full, suffix-stripped, bare
# first word) across every path/TLD combo, all fetched concurrently, and
# takes the first hit in priority order. If the precise (full) token's
# domain times out while an ambiguous truncated token's domain answers —
# "galaxydiagnostics.com" dead, "galaxy.com" live — that unrelated
# company's board wins outright ("Galaxy Diagnostics" -> a 57-job board at
# the fintech Galaxy.com, zero of them local, misreported as a live
# no-local-jobs board instead of the wrong company it actually is). A hit
# reached only through a risky token (names.risky_domain_tokens) must
# corroborate against the page content before it's trusted.

def _risky_token_in_url(url, name):
    """The risky domain token (names.risky_domain_tokens) `url`'s host
    was built from, or "" if the host isn't one of those — including when
    it's ALSO reachable via a safe (full/suffix-stripped) token, since the
    full token containing the risky one as a substring
    ("galaxydiagnostics" contains "galaxy") must not itself count as risky.

    >>> _risky_token_in_url("https://www.galaxy.com/careers", "Galaxy Diagnostics")
    'galaxy'
    >>> _risky_token_in_url("https://www.galaxydiagnostics.com/careers", "Galaxy Diagnostics")
    ''
    >>> _risky_token_in_url("https://www.unitedtherapeutics.com/careers", "United Therapeutics")
    ''
    """
    risky = risky_domain_tokens(name)
    if not risky:
        return ""
    host = re.sub(r"^https?://", "", url.lower()).split("/", 1)[0]
    safe = [t for t in domain_tokens(name) if t not in risky]
    if any(s and s in host for s in safe):
        return ""
    for t in risky:
        if t and t in host:
            return t
    return ""


def _corroborates(text, name, skip_token=""):
    """True if `text` actually mentions `name` beyond the (possibly
    generic/truncated) domain token that reached it — the check a
    risky-token hit (see _risky_token_in_url) must pass before it's
    trusted. Requires a distinctive word (>=4 letters) from `name`, other
    than `skip_token`, to appear in the text.

    >>> _corroborates("Careers at Galaxy Diagnostics", "Galaxy Diagnostics", "galaxy")
    True
    >>> _corroborates("Galaxy Digital hires blockchain engineers",
    ...                "Galaxy Diagnostics", "galaxy")
    False
    >>> _corroborates("", "Galaxy Diagnostics", "galaxy")
    False

    A name with nothing left to check (every word is the skipped token, or
    too short) doesn't block the hit — there's no more precision to ask
    for:

    >>> _corroborates("anything", "Q2", "q2")
    True
    """
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
            if len(w) >= 4 and w != skip_token]
    if not words:
        return True
    blob = (text or "").lower()
    return any(w in blob for w in words)


# ─── Detection ───────────────────────────────────────────────────────────

def _detect(text, final_url=""):
    """Scan text + final URL for an ATS signature.

    Returns (kind, ats, slug) where kind is "fetchable" | "semi" | "lead",
    or None. Workday first (triple, highest confidence), then ADP (two
    params, generic host), then single-capture platforms.
    """
    blob = f"{final_url}\n{text}"
    triple = _extract_workday_triple(blob)
    if triple:
        return "fetchable", "workday", triple
    if "workforcenow.adp.com" in blob.lower():
        unescaped = html.unescape(blob)
        cid = _ADP_CID_RE.search(unescaped)
        ccid = _ADP_CCID_RE.search(unescaped)
        if cid and ccid:
            return "fetchable", "adp", f"{cid.group(1)}|{ccid.group(1)}"
    # UKG Pro (UltiPro): slug is CODE|GUID from the board URL.
    ukg = re.search(r"recruiting2?\.ultipro\.com/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F\-]{36})", blob, re.I)
    if ukg:
        return "fetchable", "ultipro", f"{ukg.group(1)}|{ukg.group(2)}"
    # PeopleAdmin: only the HOSTED tenants carry a signature. A university
    # serving the same software from its own hostname (jobs.ncsu.edu) is
    # indistinguishable from any other careers page here and still has to be
    # registered by hand — see local_sourcing.add_board.
    pa = _PEOPLEADMIN_RE.search(blob)
    if pa and pa.group(1).lower() not in _BAD_SUBDOMAINS:
        return "semi", "peopleadmin", pa.group(1)
    for kind, patterns in (("fetchable", ATS_LINK_PATTERNS),
                           ("semi", SEMI_FETCHABLE_PATTERNS),
                           ("lead", ATS_LEAD_PATTERNS)):
        for ats, rx in patterns:
            m = rx.search(blob)
            if not m:
                continue
            slug = m.group(1)
            if kind != "lead" and slug.lower() in _BAD_SUBDOMAINS:
                continue
            if slug and len(slug) >= 2:
                return kind, ats, slug
    return None


def _confirm_coords(ats, slug):
    """Get a live job count for sniffed coordinates. Returns int or None."""
    if ats == "adp":
        cid, _, ccid = slug.partition("|")
        try:
            r = SESSION.get(
                "https://workforcenow.adp.com/mascsr/default/careercenter"
                "/public/events/staffing/v1/job-requisitions",
                params={"cid": cid, "ccId": ccid, "locale": "en_US", "$top": 1},
                timeout=12, headers={**HEADERS, "Accept": "application/json"},
            )
            if r.status_code != 200:
                return None
            return int(r.json().get("meta", {}).get("totalNumber", 0) or 0)
        except Exception:
            return None
    probe = PROBES.get(ats)
    if not probe:
        return None
    ok, count = probe(slug)
    return count if ok else None


# Hosts that refused a connection outright (DNS failure, TLS handshake
# failure, connect timeout) this run, host -> time recorded. The candidate
# list tries ~5 paths per name-guessed host, and the miss path re-derives
# the list up to three more times (root scan, careers sniff, diagnosis), so
# one dead host cost 7 identical GETs per name in the 2026-09-01 add-names
# run. A refused connection says nothing path-specific: skip the host for a
# while. HTTP errors and READ timeouts are not cached — a slow or 404ing
# host may still answer another path.
_DEAD_HOSTS = {}
_DEAD_HOST_TTL = 15 * 60
_DEAD_HOSTS_LOCK = threading.Lock()


def _dead_host(url):
    """The host of `url` if a connection to it was refused within the TTL."""
    m = re.match(r"https?://([^/]+)", url or "")
    host = m.group(1).lower() if m else ""
    with _DEAD_HOSTS_LOCK:
        t = _DEAD_HOSTS.get(host)
        if t is not None and time.time() - t < _DEAD_HOST_TTL:
            return host
        _DEAD_HOSTS.pop(host, None)
    return ""


def _mark_dead_host(url):
    m = re.match(r"https?://([^/]+)", url or "")
    if m:
        with _DEAD_HOSTS_LOCK:
            _DEAD_HOSTS[m.group(1).lower()] = time.time()


# Per-URL outcome memo, url -> (time recorded, Response or None). The
# stages of one name's resolution (careers sniff, root scan, lead sniff,
# diagnosis) each rebuild the candidate list and fetch it again, so a LIVE
# host answered the same GET up to seven times per name (sgs.com, intertek.
# com, and a 403ing infosys.com in the 2026-09-01 add-names runs). Same
# URL, same run, same answer: hand back the first one. Bounded so a long
# discovery run can't hoard page bodies.
_PAGE_MEMO = {}
_PAGE_MEMO_CAP = 512
_PAGE_MEMO_MAX_BYTES = 2 * 1024 * 1024


def _memo_get(url):
    with _DEAD_HOSTS_LOCK:
        hit = _PAGE_MEMO.get(url)
        if hit is None:
            return False, None
        if time.time() - hit[0] >= _DEAD_HOST_TTL:
            del _PAGE_MEMO[url]
            return False, None
        return True, hit[1]


def _memo_put(url, resp):
    if resp is not None and len(resp.content or b"") > _PAGE_MEMO_MAX_BYTES:
        return
    with _DEAD_HOSTS_LOCK:
        if len(_PAGE_MEMO) >= _PAGE_MEMO_CAP:
            oldest = min(_PAGE_MEMO, key=lambda u: _PAGE_MEMO[u][0])
            del _PAGE_MEMO[oldest]
        _PAGE_MEMO[url] = (time.time(), resp)


def _fetch_page(url, timeout=6):
    """GET one careers-page candidate. Short timeout: most are speculative
    domain/path guesses that 404 or don't resolve; a real careers page
    answers fast. Returns the Response on 200 with real content, else None.
    Outcomes are memoized per URL for the run (see _PAGE_MEMO), and a host
    that refused a connection is skipped outright (see _DEAD_HOSTS)."""
    known, resp = _memo_get(url)
    if known:
        return resp
    if _dead_host(url):
        _log.debug("skip %s: host refused a connection earlier this run", url)
        return None
    try:
        r = SESSION.get(url, timeout=timeout, headers=HEADERS, allow_redirects=True)
        resp = r if r.status_code == 200 and len(r.text) >= 300 else None
    except requests.exceptions.ConnectionError:
        # requests' ConnectionError covers DNS failure, SSLError and
        # ConnectTimeout; ReadTimeout is a Timeout, not a ConnectionError.
        _mark_dead_host(url)
        return None
    except Exception:
        return None
    _memo_put(url, resp)
    return resp


def _fetch_all(urls):
    """Fetch candidates concurrently (a miss otherwise pays ~12 sequential
    GETs — the dominant per-candidate latency in a bulk run); results are
    evaluated in priority order regardless of completion order.

    These are GUESSES — `<token>.io`, `<token>.co`, `careers.<token>.com` —
    so their robots.txt failures are expected and say nothing worth logging;
    `robots.quiet()` keeps the notice for hosts we actually mean to crawl.
    """
    from scrapers import robots
    with robots.quiet():
        with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
            return dict(zip(urls, pool.map(_fetch_page, urls)))


def _pack(ats, slug, careers_url):
    """A detection -> the coordinate dict every resolver consumes.

    Workday's coordinates are a (tenant, pod, site) triple, so they travel
    under `triple`; every other platform has one slug:

    >>> _pack("greenhouse", "acme", "https://acme.com/careers")["slug"]
    'acme'
    >>> _pack("workday", ("acme", 5, "External"), "")["triple"]
    ('acme', 5, 'External')

    PeopleAdmin rows are keyed on `careers_url` rather than on the slug
    (core.store.board_key), so a hosted tenant's URL is reduced to its
    origin — whichever page of the tenant carried the signature, the board
    comes out the same:

    >>> _pack("peopleadmin", "unc",
    ...       "https://unc.peopleadmin.com/postings/search?x=1")["careers_url"]
    'https://unc.peopleadmin.com'

    Nothing else is rewritten, including a PeopleAdmin tenant on its own
    hostname, which never matches the signature and reaches the store by
    hand instead:

    >>> _pack("custom", None, "https://jobs.ncsu.edu/")["careers_url"]
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


# Tokens that appear in tenant/site strings for structural reasons and say
# nothing about WHOSE board it is.
_BOARD_GENERIC = {"jobs", "job", "careers", "career", "external", "site",
                  "portal", "search", "global", "en", "us", "www", "com"}
_NAME_GENERIC = {"inc", "llc", "ltd", "plc", "corp", "corporation", "co",
                 "the", "and", "of", "gmbh", "ag", "sa"}

# (name, tenant) pairs whose foreign-board verdict was already printed this
# process — the verdicts themselves are cached in core.claude.
_FOREIGN_ANNOUNCED = set()


def _tenant_affinity(name, triple):
    """True if a sniffed Workday (tenant, pod, site) shares an identity
    token with the company name — tenant OR site, either direction, or a
    4+-char shared prefix (tenants abbreviate: 'vhr-unither').

    >>> _tenant_affinity("KBI Biopharma", ("jsrglobal", 1, "KBI_Biopharma"))
    True
    >>> _tenant_affinity("Bioventus", ("osv-bioventus", 501, "External"))
    True
    >>> _tenant_affinity("United Therapeutics", ("vhr-unither", 5, "External"))
    True

    No affinity does NOT mean wrong — Merck & Co. really posts on tenant
    'msd' — it means "cannot be confirmed from the strings alone", which is
    what routes the hit to _foreign_board's LLM check:

    >>> _tenant_affinity("Genedata", ("danaher", 1, "DanaherJobs"))
    False
    >>> _tenant_affinity("Merck & Co.", ("msd", 5, "SearchJobs"))
    False
    """
    tenant, _, site = triple
    board = f"{tenant} {re.sub(r'([a-z])([A-Z])', r'\\1 \\2', str(site))}"
    board_toks = [t for t in re.findall(r"[a-z0-9]+", board.lower())
                  if len(t) >= 3 and t not in _BOARD_GENERIC]
    name_words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())
                  if w not in _NAME_GENERIC]
    squashed_name = "".join(name_words)
    squashed_board = "".join(board_toks)
    for bt in board_toks:
        if bt in squashed_name:
            return True
    for nw in name_words:
        if len(nw) >= 3 and nw in squashed_board:
            return True
    for bt in board_toks:
        for nw in name_words:
            if len(bt) >= 5 and len(nw) >= 5 and bt[:4] == nw[:4]:
                return True
    return False


def _foreign_board(name, triple):
    """True when a sniffed Workday triple should NOT be attributed to
    `name`: the strings share no identity token AND the LLM judges the
    tenant to be another employer's (typically a parent conglomerate's
    shared board).

    Notes:
        Genedata's careers page legitimately links to Danaher's
        danaher/DanaherJobs board — but confirming that board AS Genedata
        made the daily crawl ingest every Danaher opco's local job under
        Genedata's name (2026-08-28 discover session, nc=28). The string
        check alone can't reject it: Merck & Co. really does post on
        tenant 'msd', so a bare mismatch must stay (that's also the
        offline behavior — with no API key the verdict is unknown and the
        hit is kept, flagged in the log for a human glance).
    """
    if _tenant_affinity(name, triple):
        return False
    from core.claude import board_is_own
    own = board_is_own(name, triple[0], triple[2])
    # Announce each (name, board) verdict ONCE — the sniff scans many
    # candidate URLs that embed the same board link, and the 2026-08-28
    # discover log repeated the same skip line 3x per company. Single write,
    # not print(): this runs on sniff worker threads, and print()'s separate
    # text/newline writes let another thread splice its line into this one.
    key = (name, triple[0])
    if own is False:
        if key not in _FOREIGN_ANNOUNCED:
            _FOREIGN_ANNOUNCED.add(key)
            sys.stdout.write(
                f"    [!] {name}: sniffed Workday board {triple[0]}/"
                f"{triple[2]} belongs to another employer (parent/shared "
                f"board) - skipped\n")
        return True
    if own is None and key not in _FOREIGN_ANNOUNCED:
        _FOREIGN_ANNOUNCED.add(key)
        sys.stdout.write(
            f"    [?] {name}: Workday tenant {triple[0]!r} shares no token "
            f"with the name and can't be verified offline - keeping; worth "
            f"a human glance\n")
    return False


# ─── Public API ──────────────────────────────────────────────────────────

def sniff_ats(name, careers_url="", timeout=6):
    """Raw detection: first fetchable/semi-fetchable ATS found, else a
    custom self-hosted board, else None. Shape:
    {"ats", "slug"|"triple", "careers_url"}."""
    urls = candidate_urls(name, careers_url)
    if not urls:
        return None
    responses = _fetch_all(urls)
    _log.debug("sniff %s: %d candidate URL(s), %d answered", name, len(urls),
               sum(1 for r in responses.values() if r is not None))
    custom = None
    for url in urls:
        r = responses.get(url)
        if r is None:
            continue
        # A hit reached only through a truncated/generic domain guess
        # ("galaxy.com" for "Galaxy Diagnostics") must corroborate against
        # the page before it's trusted — otherwise the precise domain
        # timing out hands the whole result to an unrelated company.
        risky_tok = _risky_token_in_url(url, name)
        if risky_tok and not _corroborates(r.text, name, risky_tok):
            continue
        hit = _detect(r.text, r.url)
        if hit and hit[0] in ("fetchable", "semi"):
            if hit[1] == "workday" and _foreign_board(name, hit[2]):
                hit = None      # keep scanning; the custom fallback may
            else:               # still capture the company's OWN listings
                _log.debug("sniff %s: %s %r found on %s",
                           name, hit[1], hit[2], r.url)
                return _pack(hit[1], hit[2], r.url)
        if custom is None:
            # Custom board: resolve to the page that actually holds the
            # listings (this page, or the openings page one hop away).
            from scrapers.fetchers.company import custom_board_listing_url
            listing = custom_board_listing_url(r.url, r.text)
            if listing:
                custom = {"ats": "custom", "careers_url": listing}
    # Every careers-path candidate missed: a company whose ATS badge sits on
    # the homepage itself (no dedicated /careers page -- see _scan_root)
    # still has one more place to look before this is a miss.
    root_hit = _scan_root(name, careers_url)
    if root_hit:
        return root_hit
    return custom


def sniff_careers_ats(name, careers_url=""):
    """Pipeline style: prefer coordinates we can CONFIRM with a live count;
    otherwise surface the highest-priority detection as a lead."""
    urls = candidate_urls(name, careers_url)
    if not urls:
        return None
    responses = _fetch_all(urls)
    lead = None  # first (highest-priority) unconfirmable detection seen
    for url in urls:
        r = responses.get(url)
        if r is None:
            continue
        risky_tok = _risky_token_in_url(url, name)
        if risky_tok and not _corroborates(r.text, name, risky_tok):
            continue
        hit = _detect(r.text, r.url)
        if not hit:
            continue
        kind, ats, slug = hit
        if ats == "workday" and _foreign_board(name, slug):
            continue
        if kind == "fetchable" and ats != "workday":
            count = _confirm_coords(ats, slug)
            if count is not None:
                return {"confirmed": True, "ats": ats, "slug": slug,
                        "count": count, "source_url": r.url}
        if lead is None:
            lead_slug = "|".join(map(str, slug)) if isinstance(slug, tuple) else slug
            lead = {"confirmed": False, "ats": ats, "slug": lead_slug,
                    "source_url": r.url}
    if lead:
        return lead
    # No candidate careers-path yielded even an unconfirmable lead -- try the
    # bare homepage (see sniff_ats's matching fallback / _scan_root).
    root_hit = _scan_root(name, careers_url)
    if root_hit:
        ats, slug = root_hit["ats"], root_hit.get("slug", root_hit.get("triple"))
        count = _confirm_coords(ats, slug) if ats != "workday" else None
        if count is not None:
            return {"confirmed": True, "ats": ats, "slug": slug,
                    "count": count, "source_url": root_hit["careers_url"]}
        slug_str = "|".join(map(str, slug)) if isinstance(slug, tuple) else slug
        return {"confirmed": False, "ats": ats, "slug": slug_str,
                "source_url": root_hit["careers_url"]}
    return None


# ─── "no-board-found" subcategories ───────────────────────────────────────
#
# A bare "no-board-found" means "we don't know why" -- which of the very
# different failure modes below it was is invisible until someone probes by
# hand. diagnose_no_board turns the sniff's own fetch results into one of
# four qualifiers (discovery.local_sourcing.classify_miss appends it to the
# "no-board-found" family, e.g. "no-board-found:site-only-no-careers").

def diagnose_no_board(name, careers_url=""):
    """Why sniff_careers_ats found nothing for `name`, one of:

    - "domain-unreachable": not one candidate URL answered at all (DNS/SSL/
      timeout on every guess) -- likely defunct or acquired.
    - "wrong-domain": every page that DID answer was reached only through a
      truncated/generic domain token (see names.risky_domain_tokens) and
      none corroborated the company name -- the precise domain never
      answered, so all we have is someone else's page (the galaxy.com
      shape: "galaxydiagnostics.com" dead, "galaxy.com" live).
    - "careers-page-no-ats": at least one legitimately-reached page (a safe
      token, or a risky one that DID corroborate) looks like a real
      self-hosted job board (>=3 genuine job-detail links), but no
      recognized ATS is embedded on it.
    - "site-only-no-careers": at least one legitimately-reached page
      answered, but none of them is a careers page or has a detectable
      ATS -- the domain resolves, nothing else does.

    A risky-token hit is judged only when nothing safer answered: if the
    real domain (or any corroborating page) responds too, a coincidental
    unrelated site at a truncated-token guess is just noise, not evidence
    this company's domain is wrong.

    A name with no domain tokens to guess and no careers_url hint has no
    candidate URL to even attempt:

    >>> diagnose_no_board("")
    'domain-unreachable'

    The other three qualifiers all need a live fetch to demonstrate (a real
    candidate response, not just an empty candidate list), so they are
    covered by tests/test_parsers.py::TestDiagnoseNoBoard instead of a
    doctest here.

    Notes:
        Costs its own fetch pass (re-derives the candidate list rather than
        reusing sniff_careers_ats's), so it is called only on the already-
        established failure path in classify_miss, never per candidate in
        a bulk pass.
    """
    urls = (candidate_urls(name, careers_url)
            + candidate_urls(name, careers_url, patterns=ROOT_PATTERNS, cap=None))
    if not urls:
        return "domain-unreachable"
    responses = _fetch_all(urls)
    hits = [(u, r) for u, r in responses.items() if r is not None]
    if not hits:
        return "domain-unreachable"
    safe_hits, saw_risky_uncorroborated = [], False
    for url, r in hits:
        risky_tok = _risky_token_in_url(url, name)
        if risky_tok and not _corroborates(r.text, name, risky_tok):
            saw_risky_uncorroborated = True
            continue
        safe_hits.append(r)
    if not safe_hits:
        return "wrong-domain" if saw_risky_uncorroborated else "domain-unreachable"
    if any(_looks_like_custom_board(r.text) for r in safe_hits):
        return "careers-page-no-ats"
    return "site-only-no-careers"


# ─── Headless-browser sniffer (JS-rendered careers pages) ────────────────

class JsSniffer:
    """
    Headless-browser ATS sniffer for JS-rendered careers pages (Teleflex,
    Siemens Healthineers, etc.) whose ATS link only appears after JS runs.
    Reuses one browser across calls. Degrades to no-op if Playwright is
    missing. Use as a context manager; call from a single thread.
    """

    def __init__(self):
        self._pw = self._browser = self._page = None
        self._ok = True

    def _ensure(self):
        if self._page or not self._ok:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
            from config import BROWSER_UA
            from .probes import launch_chromium
            self._pw = sync_playwright().start()
            self._browser, _ = launch_chromium(self._pw, headless=True)
            self._page = self._browser.new_context(
                user_agent=BROWSER_UA, viewport={"width": 1440, "height": 900},
                locale="en-US").new_page()
        except Exception as e:
            # Same one-shot reporting as the Workday JS probe — a missing
            # browser is one condition, not one per instance.
            from .probes import _js_launch_hint, _report_js_disabled
            _report_js_disabled(f"careers-page sniff: {_js_launch_hint(e)}")
            self._ok = False
        return self._page

    def sniff(self, name, careers_url=""):
        page = self._ensure()
        if not page:
            return None
        for url in candidate_urls(name, careers_url):
            risky_tok = _risky_token_in_url(url, name)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                continue
            for _ in range(2):
                try:
                    content = page.content()
                    hit = _detect(content, page.url)
                except Exception:
                    content, hit = "", None
                if hit and hit[0] in ("fetchable", "semi"):
                    if risky_tok and not _corroborates(content, name, risky_tok):
                        break
                    return _pack(hit[1], hit[2], page.url)
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    break
        return None

    def close(self):
        for obj in (self._browser, self._pw):
            try:
                obj and (obj.close() if obj is self._browser else obj.stop())
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# Back-compat: modules that imported the signature table from here.
_SIGS = ATS_LINK_PATTERNS + SEMI_FETCHABLE_PATTERNS
