"""ATS slug probes — cheap HEAD/GET checks to confirm a slug is real."""

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from ..http import HEADERS


def probe_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        return (True, len(r.json().get("jobs", [])))
    except Exception:
        return (False, 0)


def probe_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        data = r.json()
        return (True, len(data) if isinstance(data, list) else 0)
    except Exception:
        return (False, 0)


def probe_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        return (True, len(r.json().get("jobPostings", [])))
    except Exception:
        return (False, 0)


def probe_kula(slug, retries=1):
    # Kula serves a full HTML page (no JSON API) and throttles under
    # probe bursts — a confirmed-live board can 4xx/timeout once during
    # a parallel discovery run. One retry with a short backoff recovers
    # those without slowing genuine misses much.
    url = f"https://careers.kula.ai/{slug}"
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=10, headers=HEADERS)
            if r.status_code == 200 and len(r.text) > 1000:
                return (True, 0)
        except Exception:
            pass
        if attempt < retries:
            time.sleep(1.0)
    return (False, 0)


def probe_jazzhr(slug):
    url = f"https://{slug}.applytojob.com/"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        n = len(re.findall(r"/apply/[A-Za-z0-9]+/", r.text))
        return (n > 0, n)
    except Exception:
        return (False, 0)


def probe_bamboohr(slug):
    url = f"https://{slug}.bamboohr.com/careers/list"
    try:
        r = requests.get(url, timeout=10,
                         headers={**HEADERS, "Accept": "application/json"})
        if r.status_code != 200:
            return (False, 0)
        return (True, len(r.json().get("result", []) or []))
    except Exception:
        return (False, 0)


PROBES = {
    "greenhouse": probe_greenhouse,
    "lever":      probe_lever,
    "ashby":      probe_ashby,
    "kula":       probe_kula,
    "jazzhr":     probe_jazzhr,
    "bamboohr":   probe_bamboohr,
}


# ─── Workday (separate signature — needs name + careers URL hint) ────────
#
# Workday URLs are a tenant+pod+site triple we can't derive from the
# company name alone (e.g. redhat.wd5.myworkdayjobs.com/Jobs_External).
# So probe_workday scrapes the company's careers page looking for a
# myworkdayjobs.com link, then validates the triple against the CXS
# search API to get a live job count.
#
# Because the signature differs from the other probes, this one is NOT
# in PROBES — validate_candidate calls it explicitly as a fallback.

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


def _extract_workday_triple(text: str):
    """
    Return (tenant, wd_pod_int, site) from the first Workday URL found
    in `text`, or None. Checks the CXS API form first (higher signal),
    then falls back to any public board URL.
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


_URL_CAP = 12

# Corporate suffixes to strip when building domain-token guesses — the
# domain rarely includes these (redhat.com, not redhatinc.com). Keep in
# sync with pipeline._CORP_SUFFIXES.
_DOMAIN_STOPWORDS = {
    "inc", "incorporated", "corp", "corporation", "ltd", "llc", "co",
    "company", "technologies", "systems", "therapeutics", "biosciences",
    "pharmaceuticals", "pharma", "sciences", "bio", "labs", "group",
    "health", "holdings",
}


def _name_domain_tokens(name: str) -> list[str]:
    """
    Guess the company's likely domain token(s) from its name. Returns
    a short list in priority order: suffix-free joined form first
    ("unitedtherapeutics" → yes, "unitherapeuticsinc" → no), then the
    first word ("united"), then the fully-joined form as a last resort.
    """
    if not name:
        return []
    # Drop parentheticals and non-letter punctuation first.
    clean = re.sub(r"\s*\([^)]*\)", "", name).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", clean) if w]
    if not words:
        return []
    # Full joined form ("unitedtherapeutics")
    full = "".join(words)
    # Suffix-stripped ("redhat" from "Red Hat Inc")
    kept = [w for w in words if w not in _DOMAIN_STOPWORDS]
    stripped = "".join(kept)
    # First-word only ("united")
    first = kept[0] if kept else words[0]

    # Priority: full joined form first ("unitedtherapeutics.com" beats
    # the ambiguous "united.com"), then suffix-stripped, then first word.
    out, seen = [], set()
    for t in (full, stripped, first):
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _workday_candidate_urls(name: str, careers_url: str) -> list[str]:
    """
    Careers-page URLs to scrape, in priority order. Caps at _URL_CAP to
    keep misses bounded — we stop at the first workday URL we find, so
    high-priority URLs (hints, common paths) go first.
    """
    urls: list[str] = []
    if careers_url:
        urls.append(careers_url)

    # Paths in priority order. /en/jobs catches Red Hat; /careers is
    # the dominant pattern; /jobs catches a few odd ducks.
    paths = ("/careers", "/en/jobs", "/jobs", "/careers/",
             "/en/careers", "/company/careers/")

    for token in _name_domain_tokens(name):
        for path in paths:
            urls.append(f"https://www.{token}.com{path}")
        # Bare subdomains
        urls.append(f"https://careers.{token}.com/")
        urls.append(f"https://jobs.{token}.com/")
        urls.append(f"https://www.{token}.com/")

    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= _URL_CAP:
            break
    return out


def _count_workday_jobs(tenant: str, wd_pod: int, site: str):
    """
    POST the Workday CXS /jobs endpoint to validate the triple and
    learn the posting count. Returns an int on success, None on any
    transport/parse failure (i.e. "URL structure looked right but we
    couldn't confirm it's live").
    """
    api = (f"https://{tenant}.wd{wd_pod}.myworkdayjobs.com"
           f"/wday/cxs/{tenant}/{site}/jobs")
    try:
        r = requests.post(
            api,
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            timeout=12,
            headers={
                **HEADERS,
                "Accept":       "application/json",
                "Content-Type": "application/json",
            },
        )
        if r.status_code != 200:
            return None
        return int(r.json().get("total", 0) or 0)
    except Exception:
        return None


def probe_workday(name: str, careers_url: str = ""):
    """
    Discover a Workday tenant/pod/site for `name` by fetching likely
    careers pages and scanning for a myworkdayjobs.com link. On hit,
    validates with the CXS API.

    Returns dict {tenant, wd_pod, site, count, validated, source_url}
    or None if no workday URL was found.

    `validated=False` means the URL pattern was found but the CXS API
    didn't confirm (DNS/auth/rate-limit) — still worth surfacing to
    the user so they can wire it up manually.
    """
    for url in _workday_candidate_urls(name, careers_url):
        try:
            r = requests.get(
                url, timeout=6, headers=HEADERS, allow_redirects=True,
            )
        except Exception:
            continue
        if r.status_code != 200:
            continue
        # Workday login redirects usually land on the wd host — check
        # the final URL first, then fall through to HTML body.
        triple = _extract_workday_triple(r.url) or _extract_workday_triple(r.text)
        if not triple:
            continue
        tenant, wd_pod, site = triple
        count = _count_workday_jobs(tenant, wd_pod, site)
        return {
            "tenant":     tenant,
            "wd_pod":     wd_pod,
            "site":       site,
            "count":      count or 0,
            "validated":  count is not None,
            "source_url": r.url,
        }
    return None


# ─── Careers-page ATS sniffer ────────────────────────────────────────────
#
# Many companies (Synchron on ADP, Cognixion on BambooHR, Paradromics on
# JazzHR) aren't reachable by guessing a slug against the four JSON ATSes —
# their boards live on platforms keyed by an opaque subdomain or GUID we
# can't derive from the name. But the company's own careers page links to
# the board. So when slug probing misses, fetch the careers page(s) and
# read the ATS coordinates straight out of the embed URL.
#
# sniff_careers_ats returns one of:
#   {"confirmed": True,  "ats", "slug", "count", "source_url"}
#       — a fetchable board we validated and can add to config.
#   {"confirmed": False, "ats", "slug", "source_url"}
#       — a LEAD: the careers page links to a known-but-not-fetchable
#         platform (Eightfold, Dayforce, iCIMS, SmartRecruiters, ...).
#         Those are bot-protected/JS-only, so we can't auto-pull jobs, but
#         surfacing the exact platform + URL turns a blind miss into an
#         actionable "add this manually" note.
#   None — nothing found.

# Fetchable platforms: regex captures the board slug; sniff confirms via a
# live count. ADP needs two params (cid, ccId), handled specially below.
_ATS_LINK_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([a-z0-9_-]+)", re.I)),
    ("lever",      re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("ashby",      re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)", re.I)),
    ("kula",       re.compile(r"careers\.kula\.ai/([a-z0-9_-]+)", re.I)),
    ("jazzhr",     re.compile(r"([a-z0-9-]+)\.applytojob\.com", re.I)),
    ("bamboohr",   re.compile(r"([a-z0-9-]+)\.bamboohr\.com", re.I)),
]
_ADP_CID_RE  = re.compile(r"[?&]cid=([0-9a-f-]{8,})", re.I)
_ADP_CCID_RE = re.compile(r"[?&]ccid=([0-9A-Za-z_]+)", re.I)

# Detection-only platforms: real ATSes we can recognize but not reliably
# auto-fetch (bot-protected APIs or JS-only boards). Each regex captures a
# short identifying host/path for the lead note.
_ATS_LEAD_PATTERNS = [
    ("eightfold",       re.compile(r"([a-z0-9-]+\.eightfold\.ai)", re.I)),
    ("dayforce",        re.compile(r"(dayforcehcm\.com/[a-zA-Z-]+/[a-zA-Z0-9_-]+)", re.I)),
    ("icims",           re.compile(r"([a-z0-9-]+\.icims\.com)", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("workable",        re.compile(r"(apply\.workable\.com/[a-z0-9-]+)", re.I)),
    ("recruitee",       re.compile(r"([a-z0-9-]+\.recruitee\.com)", re.I)),
    ("teamtailor",      re.compile(r"([a-z0-9-]+\.teamtailor\.com)", re.I)),
    ("jobvite",         re.compile(r"(jobs\.jobvite\.com/[a-z0-9-]+)", re.I)),
    ("successfactors",  re.compile(r"([a-z0-9-]+\.(?:successfactors|sapsf)\.com)", re.I)),
    ("taleo",           re.compile(r"([a-z0-9-]+\.taleo\.net)", re.I)),
    ("ukg",             re.compile(r"([a-z0-9-]+\.ultipro\.com)", re.I)),
    ("paylocity",       re.compile(r"(recruiting\.paylocity\.com/[A-Za-z0-9/_-]+)", re.I)),
    ("paycom",          re.compile(r"(paycomonline\.net/[A-Za-z0-9/_-]+)", re.I)),
    ("breezy",          re.compile(r"([a-z0-9-]+\.breezy\.hr)", re.I)),
    ("gohire",          re.compile(r"([a-z0-9-]+\.gohire\.io)", re.I)),
    # NOTE: Workday is intentionally NOT here — it's fetchable via the CXS
    # API (probe_workday confirms with a live count), so it must stay a
    # confirmable path, not a detection-only lead. Leaving it here would
    # make validate_candidate's "skip workday fallback when a lead exists"
    # guard suppress the very probe that confirms it.
]

# Fetchable-ATS host detector — used to skip Claude's careers_url when it's
# itself a dead slug-guess against a JSON ATS (already covered by probing).
_FETCHABLE_HOST_RE = re.compile(
    r"(greenhouse\.io|lever\.co|ashbyhq\.com|kula\.ai|applytojob\.com|bamboohr\.com)",
    re.I,
)

_SNIFF_URL_CAP = 16
_CAREERS_TLDS  = (".com", ".co", ".io", ".ai")
_CAREERS_PATHS = ("/careers", "/careers/", "/jobs", "/careers/open-positions",
                  "/about/careers", "/company/careers", "/join", "/join-us",
                  "/open-positions", "/employment")


def _careers_urls(name, careers_url):
    """Careers-page URLs to sniff, in priority order (caps at _SNIFF_URL_CAP).

    Ordered by likelihood: Claude's careers_url (unless it's a dead JSON-ATS
    guess), then .com careers/jobs per name token, then alternate TLDs
    (.co/.io/.ai — common for neurotech startups), then longer-tail paths
    and careers/jobs subdomains.
    """
    urls = []
    if careers_url and not _FETCHABLE_HOST_RE.search(careers_url):
        urls.append(careers_url)

    tokens = _name_domain_tokens(name)
    # Tier 1: most likely — .com /careers and /jobs.
    for token in tokens:
        urls.append(f"https://www.{token}.com/careers")
        urls.append(f"https://{token}.com/careers")
        urls.append(f"https://www.{token}.com/jobs")
    # Tier 2: alternate TLDs, /careers.
    for token in tokens:
        for tld in _CAREERS_TLDS[1:]:
            urls.append(f"https://www.{token}{tld}/careers")
            urls.append(f"https://{token}{tld}/careers")
    # Tier 3: longer-tail paths + careers/jobs subdomains.
    for token in tokens:
        for path in _CAREERS_PATHS[3:]:
            urls.append(f"https://www.{token}.com{path}")
        urls.append(f"https://careers.{token}.com/")
        urls.append(f"https://jobs.{token}.com/")

    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= _SNIFF_URL_CAP:
            break
    return out


def _coords_from_text(text):
    """Return (ats, slug) from the first fetchable ATS embed, or None."""
    # ADP first: it needs two params and its host is generic. Unescape so
    # entity-encoded query separators ("&amp;ccid=") still match.
    if "workforcenow.adp.com" in text.lower():
        unescaped = html.unescape(text)
        cid = _ADP_CID_RE.search(unescaped)
        ccid = _ADP_CCID_RE.search(unescaped)
        if cid and ccid:
            return "adp", f"{cid.group(1)}|{ccid.group(1)}"
    for ats, pat in _ATS_LINK_PATTERNS:
        m = pat.search(text)
        if m:
            slug = m.group(1)
            # Skip obvious non-board subdomains.
            if slug.lower() in ("www", "help", "support", "blog", "app"):
                continue
            return ats, slug
    return None


def _lead_from_text(text):
    """Return (platform, host) for a detection-only ATS, or None."""
    for platform, pat in _ATS_LEAD_PATTERNS:
        m = pat.search(text)
        if m:
            return platform, m.group(1)
    return None


def _confirm_coords(ats, slug):
    """Get a live job count for sniffed coordinates. Returns int or None."""
    if ats == "adp":
        cid, _, ccid = slug.partition("|")
        try:
            r = requests.get(
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


def _fetch_careers_page(url):
    """GET one careers-page candidate. Short timeout: most are speculative
    domain/path guesses that 404 or don't resolve; a real careers page
    answers fast. Returns the Response on 200, else None."""
    try:
        r = requests.get(url, timeout=6, headers=HEADERS, allow_redirects=True)
        return r if r.status_code == 200 else None
    except Exception:
        return None


def sniff_careers_ats(name, careers_url=""):
    """
    Fetch the company's careers page(s) and read the ATS off the embedded
    board link. Prefers a fetchable confirmation; falls back to a
    detection-only lead. See module section header for the return shape.

    The candidate URLs are fetched concurrently (a miss otherwise pays ~16
    sequential GETs — the dominant per-candidate latency in a bulk run),
    then evaluated in priority order so the highest-priority confirm/lead
    still wins regardless of which response landed first.
    """
    urls = _careers_urls(name, careers_url)
    if not urls:
        return None
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        responses = dict(zip(urls, pool.map(_fetch_careers_page, urls)))

    lead = None  # first (highest-priority) detection-only platform seen
    for url in urls:
        r = responses.get(url)
        if r is None:
            continue
        blob = r.url + " " + r.text
        coords = _coords_from_text(r.url) or _coords_from_text(r.text)
        if coords:
            ats, slug = coords
            count = _confirm_coords(ats, slug)
            if count is not None:
                return {"confirmed": True, "ats": ats, "slug": slug,
                        "count": count, "source_url": r.url}
        if lead is None:
            hit = _lead_from_text(blob)
            if hit:
                lead = {"confirmed": False, "ats": hit[0], "slug": hit[1],
                        "source_url": r.url}
    return lead


# ─── JS-rendered Workday probe (fallback for SPA careers pages) ──────────
#
# Many Fortune-500 careers pages (NetApp, Cisco, Syneos, Precision
# BioSciences, WillowTree, etc.) are React/Angular SPAs — the actual
# myworkdayjobs.com link is only inserted into the DOM after JS runs, so
# the static probe_workday above can't see it.
#
# WorkdayJsProbe launches a single headless Playwright browser, reuses
# it across every candidate in a discover() run (browser startup is
# ~2-3s — not something we want to pay per candidate), and degrades
# cleanly when Playwright isn't installed. Use it as a context manager.

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from config import BROWSER_UA


class WorkdayJsProbe:
    """
    Lazy-launched headless Playwright wrapper for JS-rendered workday
    scraping. Amortizes browser startup across many candidates.

    Usage:
        with WorkdayJsProbe() as js:
            meta = js.probe("NetApp", careers_url="")

    If Playwright isn't installed or the browser fails to launch, the
    object stays disabled — `probe()` returns None quietly so the
    calling pipeline can proceed without the JS fallback.
    """

    def __init__(self):
        self._stack = ExitStack()
        self._page = None
        self._enabled = True  # flipped False after a launch failure
        self._launched = False
        # Sync Playwright binds its internal greenlet to the thread that
        # first enters sync_playwright() and MUST be torn down on that
        # same thread — otherwise close() raises greenlet.error. With a
        # thread pool dispatching probe() calls, "same thread" is only
        # guaranteed if we pin Playwright to a dedicated worker.
        #
        # One max_workers=1 executor owns every browser call: launch,
        # navigate, and close. Other worker threads submit probe()
        # requests and block on .result(), so the static probe_workday
        # paths stay fully parallel while the JS fallback is serialized
        # onto a single browser thread.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="workday-js",
        )

    # ── internals ────────────────────────────────────────────────────────

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        if not self._enabled:
            return None
        try:
            # Import locally so we don't sys.exit() when playwright isn't
            # installed — require_browser() does, which is fine for its
            # intended callers but not for an opportunistic fallback.
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("    [js] playwright not installed; JS workday probe disabled")
            self._enabled = False
            return None
        try:
            pw = self._stack.enter_context(sync_playwright())
            browser = pw.chromium.launch(headless=True)
            self._stack.callback(browser.close)
            context = browser.new_context(
                user_agent=BROWSER_UA,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
            )
            self._stack.callback(context.close)
            self._page = context.new_page()
            self._launched = True
        except Exception as e:
            print(f"    [js] Playwright browser launch failed ({e}); "
                  "JS workday probe disabled")
            self._enabled = False
            self._stack.close()
            return None
        return self._page

    @staticmethod
    def _scan(page, url: str):
        """
        Navigate + wait for JS, returning a (tenant, pod, site) triple
        or None. Has three short-circuits so we don't pay the full
        networkidle wait on obvious non-matches:
          1. Did the URL redirect straight to myworkdayjobs.com?
          2. Is the workday link in the initial server-rendered HTML?
          3. After JS settles (networkidle, capped at 6s), try again.
        """
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            msg = str(e)
            if ("interrupted by another navigation" not in msg
                    and "Navigation timeout" not in msg):
                return None
        try:
            cur = page.url
        except Exception:
            cur = ""
        if (triple := _extract_workday_triple(cur)):
            return triple
        try:
            html = page.content()
        except Exception:
            html = ""
        if (triple := _extract_workday_triple(html)):
            return triple
        # Wait for JS-deferred content (iframes, ajax-injected links).
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        try:
            cur = page.url
            html = page.content()
        except Exception:
            return None
        return _extract_workday_triple(cur) or _extract_workday_triple(html)

    # ── public API ───────────────────────────────────────────────────────

    def _probe_impl(self, name: str, careers_url: str = ""):
        """Runs entirely on the browser-owning thread."""
        page = self._ensure_page()
        if page is None:
            return None
        for url in _workday_candidate_urls(name, careers_url):
            triple = self._scan(page, url)
            if not triple:
                continue
            tenant, wd_pod, site = triple
            count = _count_workday_jobs(tenant, wd_pod, site)
            try:
                source = page.url
            except Exception:
                source = url
            return {
                "tenant":     tenant,
                "wd_pod":     wd_pod,
                "site":       site,
                "count":      count or 0,
                "validated":  count is not None,
                "source_url": source,
            }
        return None

    def probe(self, name: str, careers_url: str = ""):
        """
        Same return shape as probe_workday(), or None.

        Thread-safe: every Playwright call is dispatched onto the single
        browser-owning worker thread and the caller blocks on .result().
        Workers calling probe() concurrently queue behind each other,
        but their static probe_workday() work keeps running in parallel.
        """
        if not self._enabled:
            return None
        try:
            return self._executor.submit(
                self._probe_impl, name, careers_url,
            ).result()
        except Exception as e:
            # A browser-thread crash shouldn't poison the rest of discovery.
            print(f"    [js] probe for {name!r} errored: {e}")
            return None

    def _close_impl(self):
        self._stack.close()
        self._page = None

    def close(self):
        # Tear the browser down on the same thread that built it — else
        # Playwright raises greenlet.error. After the close lands, we can
        # safely shut the executor down.
        if self._executor is None:
            return
        try:
            if self._launched or self._page is not None:
                self._executor.submit(self._close_impl).result()
        except Exception as e:
            print(f"    [js] browser close errored: {e}")
        self._executor.shutdown(wait=True)
        self._executor = None

    @property
    def launched(self) -> bool:
        """True once the browser has actually started (for logging)."""
        return self._launched

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()
