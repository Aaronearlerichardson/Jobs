"""ATS slug probes — cheap HEAD/GET checks to confirm a slug is real."""

import queue
import re
import threading
import time

from src import config
from src.ats.signatures import extract_workday_triple
from src.match.locality import is_nc as _has_nc
from src.match.names import slug_guesses
from src.net.http import HEADERS, SESSION
from .fetchpool import candidate_urls
from .identity import _foreign_board, candidate_pages

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


# ─── The slug probes ─────────────────────────────────────────────────────
#
# Every one of these answers the same question -- does this handle name a
# real board, and how many postings are on it -- and returns the same
# (ok, count) pair. Twelve of them had been written out longhand, five
# lines of identical request-and-swallow around the one expression that
# differed. Two builders now.
#
# The interesting per-ATS decision is `require_jobs`, and writing it out
# twelve times is how it gets forgotten: SmartRecruiters answers 200 with
# totalFound:0 for ANY slug, so a 200 alone is not proof of a board, and
# every guessed slug "confirmed" with zero jobs until that was noticed.
# It is a named argument here, visible in one column.


def _api_probe(url, count, *, require_jobs=False, accept=None, headers=None,
               timeout=None, retries=0, backoff=1.0):
    """Build a `(ok, n)` probe that GETs a URL and counts what came back.

    `url` is a format string taking the handle, or a callable for a board
    whose base URL is the fetcher's to know. `count(response, handle)`
    returns the posting count; `accept(response)` replaces it for a board
    that can only be recognised, not counted. Anything unexpected -- a bad
    status, a timeout, malformed JSON -- is (False, 0): a probe reports,
    it never raises at its caller.
    """
    def probe(handle):
        for attempt in range(retries + 1):
            try:
                r = SESSION.get(url(handle) if callable(url)
                                else url.format(handle),
                                timeout=timeout or config.PROBE_TIMEOUT,
                                headers={**HEADERS, **(headers or {})})
                if r.status_code == 200:
                    if accept is not None:
                        return (True, 0) if accept(r) else (False, 0)
                    n = count(r, handle)
                    return (n > 0 if require_jobs else True, n)
            except Exception:
                pass
            if attempt < retries:
                time.sleep(backoff)
        return (False, 0)
    return probe


def _parser_probe(module):
    """Build a probe out of the fetcher's own board parser, for the ATSes
    where the fetcher already knows the URL and the payload shape. Imported
    lazily: probes.py is pulled in by discovery paths that never fetch a
    board, and these modules are not cheap.

    Their `ok` flag means "has jobs", not "board exists" -- see the
    comment in ops.prune_dead_boards, which cannot use them for that
    reason.
    """
    def probe(handle):
        try:
            mod = __import__(f"src.ats.fetchers.{module}", fromlist=["parse_board"])
            jobs = mod.parse_board(handle)
            return (len(jobs) > 0, len(jobs))
        except Exception:
            return (False, 0)
    return probe


probe_greenhouse = _api_probe(
    "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    lambda r, h: len(r.json().get("jobs", [])))

probe_lever = _api_probe(
    "https://api.lever.co/v0/postings/{}?mode=json",
    lambda r, h: len(d) if isinstance(d := r.json(), list) else 0)

probe_ashby = _api_probe(
    "https://api.ashbyhq.com/posting-api/job-board/{}",
    # Posting API key is "jobs" (not the embed payload's "jobPostings").
    lambda r, h: len((j := r.json()).get("jobs", j.get("jobPostings", []))))

# Kula serves a full HTML page (no JSON API) and throttles under probe
# bursts -- a confirmed-live board can 4xx/timeout once during a parallel
# discovery run. One retry with a short backoff recovers those without
# slowing genuine misses much. Nothing on the page is countable, so a
# substantial body is the whole signal.
probe_kula = _api_probe(
    "https://careers.kula.ai/{}", None,
    accept=lambda r: len(r.text) > 1000, retries=1)

probe_jazzhr = _api_probe(
    "https://{}.applytojob.com/",
    lambda r, h: len(_JAZZHR_APPLY_RE.findall(r.text)),
    require_jobs=True)

probe_bamboohr = _api_probe(
    "https://{}.bamboohr.com/careers/list",
    lambda r, h: len(r.json().get("result", []) or []),
    headers={"Accept": "application/json"})

probe_smartrecruiters = _api_probe(
    "https://api.smartrecruiters.com/v1/companies/{}/postings?limit=1",
    lambda r, h: int(r.json().get("totalFound", 0) or 0),
    require_jobs=True)

probe_jobvite = _api_probe(
    lambda h: f"{_jobvite().BASE}/{h}/search?p=0",
    lambda r, h: len(_jobvite().parse_listing(r.text, h)),
    require_jobs=True, timeout=10)

#: Paylocity by company GUID, UKG Pro (UltiPro) by 'CODE|GUID', Rippling
#: and HiBob by slug/tenant -- all four through the fetcher's parser.
probe_paylocity = _parser_probe("paylocity")
probe_rippling = _parser_probe("rippling")
probe_ultipro = _parser_probe("ultipro")
probe_hibob = _parser_probe("hibob")

_JAZZHR_APPLY_RE = re.compile(r"/apply/[A-Za-z0-9]+/")


def _jobvite():
    from src.ats.fetchers import jobvite
    return jobvite


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
# company name alone (e.g. redhat.wd5.myworkdayjobs.com/Jobs_External), so
# probe_workday scans the company's careers page(s) for a myworkdayjobs.com
# link (src.ats.signatures.extract_workday_triple), then validates the
# triple against the CXS search API to get a live job count.
#
# Because the signature differs from the other probes, this one is NOT
# in PROBES — pipeline.validate_candidate calls it explicitly as a fallback.


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
    (fetchpool.candidate_urls, fetched through its per-run memo) filtered to
    myworkdayjobs.com links, validated with the CXS API on a hit.

    Returns dict {tenant, wd_pod, site, count, validated, source_url}
    or None if no workday URL was found. `validated=False` means the URL
    pattern was found but the CXS API could not confirm it.

    Notes:
        Used to build and fetch its own candidate list; a hit reached only
        through a truncated domain token, or belonging to a parent
        company's shared tenant, went unchecked here while the sniffer
        rejected it. Both paths walk identity.candidate_pages now, so the
        guards cannot come apart again by editing one of them.
    """
    for r in candidate_pages(name, careers_url):
        # Workday login redirects usually land on the wd host -- check
        # the final URL first, then fall through to HTML body.
        triple = extract_workday_triple(r.url) or extract_workday_triple(r.text)
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
from src.config import BROWSER_UA


class WorkdayJsProbe:
    """
    Lazy-launched headless Playwright wrapper for JS-rendered workday
    scraping. Amortizes browser startup across many candidates.

    Usage:
        with WorkdayJsProbe() as js:
            meta = js.probe("NetApp", careers_url="")

    If Playwright isn't installed or the browser fails to launch, the
    failure is reported once per process (_report_js_disabled) and every
    later probe() returns None.
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
        if (triple := extract_workday_triple(cur)):
            return triple
        try:
            html = page.content()
        except Exception:
            html = ""
        if (triple := extract_workday_triple(html)):
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
        return extract_workday_triple(cur) or extract_workday_triple(html)

    # ── public API ───────────────────────────────────────────────────────

    def _probe_impl(self, name: str, careers_url: str = ""):
        """Runs entirely on the browser-owning thread."""
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
    K are busy) and hand it back when their scrape finishes.
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


# ─── Probing a COMPANY, not a handle ────────────────────────────
#
# The probes above take a handle someone already has. These take a NAME:
# guess its slugs (match.names.slug_guesses), try each platform, and then
# count how many of the board's postings are in your [locality] -- which
# is what rejects a slug-guess that lands on a real board belonging to
# somebody else. Store-free, like everything in this package.
#
# Lived in discovery/local_sourcing.py, which is the SOURCING half: it
# decides which names to try and writes the results to the roster. This
# is resolution, and it sat there only because that is where the caller
# was.


def _wd_search_text():
    """Free-text location term for Workday's CXS search, from [locality] —
    the same derivation the crawl fetcher uses, so a probe's count and the
    later crawl agree on what "in your area" means."""
    from src.ats.fetchers.company import _default_search_text
    return _default_search_text()


def _nc_count_greenhouse(slug):
    try:
        r = SESSION.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
                         timeout=config.PROBE_TIMEOUT, headers=HEADERS)
        return sum(1 for j in r.json().get("jobs", [])
                   if _has_nc(j.get("location", {}).get("name", "")))
    except Exception:
        return 0


def _nc_count_lever(slug):
    try:
        r = SESSION.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                         timeout=config.PROBE_TIMEOUT, headers=HEADERS)
        return sum(1 for j in r.json()
                   if _has_nc(j.get("categories", {}).get("location", "")))
    except Exception:
        return 0


def _nc_count_ashby(slug):
    try:
        r = SESSION.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                         timeout=config.PROBE_TIMEOUT, headers=HEADERS)
        data = r.json()
        return sum(1 for j in data.get("jobs", data.get("jobPostings", []))
                   if _has_nc(j.get("location", "")))
    except Exception:
        return 0


def _nc_count_workday(tenant, pod, site):
    """Count Workday postings in your [locality], scoped the way the crawl
    scopes the board (location facets, else searchText), and never taken
    at face value when the scope did not narrow anything -- see
    src.ats.fetchers.company.wd_local_count."""
    from src.ats.fetchers.company import NC_RE, wd_local_count
    try:
        return wd_local_count(tenant, pod, site, NC_RE,
                              search_text=_wd_search_text())
    except Exception:
        return 0


def probe_company(name, try_workday=True):
    """
    Probe Greenhouse/Lever/Ashby (fast) then — only if ``try_workday`` —
    Workday (slow careers-page fallback), then VERIFY the board has NC-area
    jobs (kills false-positive slug collisions and enforces local relevance).
    Returns a hit dict with an ``nc`` count, or None.
    """
    hit = None
    for slug in slug_guesses(name):
        for ats, fn, nc_fn in (("greenhouse", probe_greenhouse, _nc_count_greenhouse),
                               ("lever", probe_lever, _nc_count_lever),
                               ("ashby", probe_ashby, _nc_count_ashby)):
            ok, count = fn(slug)
            if ok:
                hit = {"name": name, "ats": ats, "slug": slug,
                       "count": count, "nc": nc_fn(slug)}
                break
        if hit:
            break
    if not hit and try_workday:
        wd = probe_workday(name)
        if wd and wd.get("validated"):
            hit = {"name": name, "ats": "workday",
                   "slug": (wd["tenant"], wd["wd_pod"], wd["site"]),
                   "count": wd["count"],
                   "nc": _nc_count_workday(wd["tenant"], wd["wd_pod"], wd["site"])}
    return hit
