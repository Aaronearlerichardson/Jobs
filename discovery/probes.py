"""ATS slug probes — cheap HEAD/GET checks to confirm a slug is real."""

import queue
import re
import threading
import time

import config

from scrapers.http import HEADERS, SESSION


# Whether the headless browser is usable is a PROCESS fact, not a per-probe
# one. The JS pass runs several WorkdayJsProbe instances in parallel, each with
# its own enabled flag, so one missing browser printed the failure once per
# instance — and Playwright's launch error embeds a ten-line ASCII banner, so
# four probes produced forty lines saying the same thing.
_JS_NOTICE_LOCK = threading.Lock()
_JS_NOTICES = set()


def _js_notice_once(key, message):
    """Print a `[js]` notice the first time `key` comes up. True if printed."""
    with _JS_NOTICE_LOCK:
        if key in _JS_NOTICES:
            return False
        _JS_NOTICES.add(key)
    print(f"    [js] {message}")
    return True


def _js_launch_hint(exc):
    """The actionable sentence from a Playwright launch error, without the box.

    Playwright renders "run `playwright install`" inside a drawn ASCII frame.
    That is helpful once and noise thereafter, and it buries the one detail
    that differs between causes.
    """
    text = str(exc)
    if "Executable doesn't exist" in text or "playwright install" in text:
        return ("browser binary missing — run `playwright install chromium` "
                "(the playwright package was upgraded without re-downloading "
                "its browsers)")
    return text.split("\n", 1)[0].strip()


def _report_js_disabled(detail):
    """Say the JS fallback is off, once per process. True if we reported."""
    return _js_notice_once("disabled", f"{detail}; JS workday probe disabled")


def _clear_js_disabled():
    """A successful launch re-arms the notice, so a later failure in a
    long-lived process (the web UI runs many passes) is still reported."""
    with _JS_NOTICE_LOCK:
        _JS_NOTICES.discard("disabled")


def launch_chromium(pw, **kwargs):
    """Launch headless Chromium, falling back to a browser the machine has.

    Playwright's own pinned build is tried first — it is the most predictable
    and the only one whose version we control. But it only exists if somebody
    ran `playwright install`, which is a separate step from `pip install` and
    so is routinely missing: on CI runners, on a fresh clone, and on any
    machine where the playwright PACKAGE was upgraded without re-downloading
    its browsers (the package pins a build number, so an upgrade silently
    invalidates the browser already on disk).

    `channel=` drives an already-installed branded browser instead. GitHub's
    hosted runners ship Chrome and Edge, and most desktops have one, so this
    turns "JS probe disabled" into "JS probe works" with no download.

    Returns (browser, channel) where channel is None for the bundled build.
    Re-raises the FIRST failure if every channel fails, because that one names
    the missing bundled build — the actionable error for someone who meant to
    run `playwright install`.
    """
    first_error = None
    for channel in config.BROWSER_CHANNELS:
        try:
            opts = dict(kwargs)
            if channel:
                opts["channel"] = channel
            browser = pw.chromium.launch(**opts)
        except Exception as e:
            if first_error is None:
                first_error = e
            continue
        if channel:
            _js_notice_once(
                f"channel:{channel}",
                f"using the system {channel} browser "
                f"(playwright's own build is not installed)")
        return browser, channel
    raise first_error


def probe_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = SESSION.get(url, timeout=config.PROBE_TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        return (True, len(r.json().get("jobs", [])))
    except Exception:
        return (False, 0)


def probe_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = SESSION.get(url, timeout=config.PROBE_TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        data = r.json()
        return (True, len(data) if isinstance(data, list) else 0)
    except Exception:
        return (False, 0)


def probe_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = SESSION.get(url, timeout=config.PROBE_TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        # Posting API key is "jobs" (not the embed payload's "jobPostings").
        data = r.json()
        return (True, len(data.get("jobs", data.get("jobPostings", []))))
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
            r = SESSION.get(url, timeout=config.PROBE_TIMEOUT, headers=HEADERS)
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
        r = SESSION.get(url, timeout=config.PROBE_TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        n = len(re.findall(r"/apply/[A-Za-z0-9]+/", r.text))
        return (n > 0, n)
    except Exception:
        return (False, 0)


def probe_bamboohr(slug):
    url = f"https://{slug}.bamboohr.com/careers/list"
    try:
        r = SESSION.get(url, timeout=config.PROBE_TIMEOUT,
                         headers={**HEADERS, "Accept": "application/json"})
        if r.status_code != 200:
            return (False, 0)
        return (True, len(r.json().get("result", []) or []))
    except Exception:
        return (False, 0)


def probe_smartrecruiters(slug):
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1"
    try:
        r = SESSION.get(url, timeout=config.PROBE_TIMEOUT, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        # SmartRecruiters returns 200 / totalFound:0 for ANY slug, even
        # nonexistent ones — so a 200 alone is not proof of a real board.
        # Require live postings (like jazzhr), else every guessed slug
        # "confirms" with zero jobs and floods discovery with false hits.
        n = int(r.json().get("totalFound", 0) or 0)
        return (n > 0, n)
    except Exception:
        return (False, 0)


def probe_paylocity(guid):
    """Confirm a Paylocity board by its company GUID (live job count from the
    embedded pageData). Reuses the fetcher's parser."""
    from scrapers.fetchers.paylocity import parse_board
    try:
        jobs = parse_board(guid)
        return (len(jobs) > 0, len(jobs))
    except Exception:
        return (False, 0)


def probe_rippling(slug):
    """Confirm a Rippling board by its slug (live job count from the public
    ATS API). Reuses the fetcher's parser."""
    from scrapers.fetchers.rippling import parse_board
    try:
        jobs = parse_board(slug)
        return (len(jobs) > 0, len(jobs))
    except Exception:
        return (False, 0)


def probe_ultipro(slug):
    """Confirm a UKG Pro (UltiPro) board by its slug ('CODE|GUID')."""
    from scrapers.fetchers.ultipro import parse_board
    try:
        jobs = parse_board(slug)
        return (len(jobs) > 0, len(jobs))
    except Exception:
        return (False, 0)


def probe_hibob(tenant):
    """Confirm a HiBob board by its tenant subdomain (live job count from
    the public job-ad API)."""
    from scrapers.fetchers.hibob import parse_board
    try:
        jobs = parse_board(tenant)
        return (len(jobs) > 0, len(jobs))
    except Exception:
        return (False, 0)


def probe_jobvite(tenant):
    """Confirm a Jobvite career site by its tenant slug (live row count
    from the server-rendered search listing). Reuses the fetcher's parser."""
    from scrapers.fetchers.jobvite import BASE, parse_listing
    try:
        r = SESSION.get(f"{BASE}/{tenant}/search", params={"p": 0},
                        timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        n = len(parse_listing(r.text, tenant))
        return (n > 0, n)
    except Exception:
        return (False, 0)


PROBES = {
    "greenhouse": probe_greenhouse,
    "lever":      probe_lever,
    "ashby":      probe_ashby,
    "kula":       probe_kula,
    "jazzhr":     probe_jazzhr,
    "bamboohr":   probe_bamboohr,
    "smartrecruiters": probe_smartrecruiters,
    "paylocity":  probe_paylocity,
    "rippling":   probe_rippling,
    "ultipro":    probe_ultipro,
    "hibob":      probe_hibob,
    "jobvite":    probe_jobvite,
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
        r = SESSION.post(
            api,
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            timeout=config.PROBE_TIMEOUT,
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
    Discover a Workday tenant/pod/site for `name`: the careers-page sniff
    (sniffer.candidate_urls, fetched through its per-run memo) filtered to
    myworkdayjobs.com links, validated with the CXS API on a hit.

    Returns dict {tenant, wd_pod, site, count, validated, source_url}
    or None if no workday URL was found. `validated=False` means the URL
    pattern was found but the CXS API could not confirm it.

    Notes:
        Used to build and fetch its own candidate list; a hit reached only
        through a truncated domain token, or belonging to a parent
        company's shared tenant, went unchecked here while the sniffer
        rejected it. Same guards on both paths now.
    """
    from .sniffer import (_corroborates, _fetch_all, _foreign_board,
                          _risky_token_in_url, candidate_urls)
    urls = candidate_urls(name, careers_url)
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
        # Workday login redirects usually land on the wd host -- check
        # the final URL first, then fall through to HTML body.
        triple = _extract_workday_triple(r.url) or _extract_workday_triple(r.text)
        if not triple or _foreign_board(name, triple):
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
            _report_js_disabled("playwright not installed")
            self._enabled = False
            return None
        try:
            pw = self._stack.enter_context(sync_playwright())
            browser, _channel = launch_chromium(pw, headless=True)
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
            _clear_js_disabled()
        except Exception as e:
            _report_js_disabled(f"browser launch failed: {_js_launch_hint(e)}")
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
        from .sniffer import candidate_urls
        page = self._ensure_page()
        if page is None:
            return None
        for url in candidate_urls(name, careers_url):
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


class WorkdayJsProbePool:
    """K headless browsers running JS Workday scrapes in parallel.

    A single WorkdayJsProbe is single-threaded by necessity — Playwright's
    sync API pins its greenlet to one thread, so one instance serializes
    every scrape onto one browser. But nothing stops running SEVERAL
    instances at once: each owns its own Playwright + browser + thread, so
    K of them give K-way parallel scraping. Discovery workers that need the
    JS fallback borrow a free browser from the pool (blocking only when all
    """

    def __init__(self, size):
        self.size = max(1, int(size))
        self._probes = [WorkdayJsProbe() for _ in range(self.size)]
        self._free = queue.Queue()
        for p in self._probes:
            self._free.put(p)

    def probe(self, name, careers_url=""):
        p = self._free.get()          # blocks until a browser is free
        try:
            return p.probe(name, careers_url)
        finally:
            self._free.put(p)

    @property
    def launched(self):
        return any(p.launched for p in self._probes)

    def close(self):
        for p in self._probes:
            p.close()

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()
