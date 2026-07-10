"""ATS slug probes — cheap HEAD/GET checks to confirm a slug is real."""

import re
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


def probe_kula(slug):
    url = f"https://careers.kula.ai/{slug}"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        return (r.status_code == 200 and len(r.text) > 1000, 0)
    except Exception:
        return (False, 0)


def probe_smartrecruiters(slug):
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        return (True, int(r.json().get("totalFound", 0) or 0))
    except Exception:
        return (False, 0)


PROBES = {
    "greenhouse":      probe_greenhouse,
    "lever":           probe_lever,
    "ashby":           probe_ashby,
    "kula":            probe_kula,
    "smartrecruiters": probe_smartrecruiters,
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
                url, timeout=12, headers=HEADERS, allow_redirects=True,
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
