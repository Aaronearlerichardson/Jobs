"""ATS slug probes — cheap HEAD/GET checks to confirm a slug is real."""

import logging
import queue
import threading
import time

from src import config
from src.ats import coords
from src.ats.board import BOARDS, board_for
from src.ats.signatures import detect
from src.match.locality import NC_RE
from src.match.names import slug_guesses
from .fetchpool import candidate_urls
from .identity import candidate_pages, foreign_board

# File-only diagnostics (session log DEBUG channel — never printed).
_log = logging.getLogger("src.discovery.resolve.probes")

# Whether the headless browser is usable is a PROCESS fact, not a per-probe
# one. The JS pass runs several JsScanProbe instances in parallel, each with
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
    return _js_notice_once("disabled", f"{detail}; JS scan probe disabled")


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


# ─── Scanned platforms (a handle no name guess reaches) ──────────────────
#
# A platform whose spec sets `discovery.scan` names its board with a handle
# the company name cannot produce (Workday's tenant, pod and site:
# redhat.wd5.myworkdayjobs.com/Jobs_External), so probe_scan reads it off
# the company's careers page(s) (`scan_hit`), then counts the board's
# listing (`Board.alive`). probe_company calls it as its last step.

#: The platforms whose spec sets `discovery.scan`, in spec order.
SCANNED = tuple(b.name for b in BOARDS.values() if b.fetchable and b.spec.discovery.scan)


def slug_keyed(board):
    """Whether `board`'s handle is the parts a detection reads (a slug,
    Workday's triple) rather than a careers URL, which a URL on the vendor's
    host does not name."""
    return "careers_url" not in board.spec.handle.columns


def confirm(ats, slug, careers_url=None):
    """A live posting count for detected coordinates, or None: the board's
    probe on the handle they name as store columns (`coords.columns`), so
    a careers_url-keyed board is probed at its careers URL."""
    b = board_for(ats)
    handle = b.handle(coords.columns(ats, slug, careers_url)) if b else None
    if not handle:
        return None
    ok, count = b.probe(handle)
    return count if ok else None


def scan_hit(text):
    """(ats, handle) of the first board of a SCANNED platform `text` names
    (`signatures.detect` restricted to each), or None."""
    for ats in SCANNED:
        hit = detect(text or "", only=ats)
        if hit:
            return ats, hit[2]
    return None


def _handle(ats, slug):
    """The engine handle for a resolver hit's slug (a tuple where the
    board spans several columns)."""
    return board_for(ats).handle(coords.columns(ats, slug))


def _scan_meta(ats, handle, source_url):
    """probe_scan's answer for `ats`'s board `handle`, found at
    `source_url`: counted through its listing, `validated` when that
    answered."""
    ok, n = board_for(ats).alive(_handle(ats, handle))
    return {"ats": ats, "slug": handle, "count": n if ok else 0,
            "validated": ok, "source_url": source_url}


def probe_scan(name: str, careers_url: str = ""):
    """
    The board of a SCANNED platform that `name`'s careers pages name
    (identity.candidate_pages, fetched through the per-run memo), counted
    through its listing.

    Returns dict {ats, slug, count, validated, source_url} or None when no
    page names one. `validated=False` means a page named the board but its
    listing did not answer.

    Notes:
        Used to build and fetch its own candidate list; a hit reached only
        through a truncated domain token, or belonging to a parent
        company's shared tenant, went unchecked here while the sniffer
        rejected it. Both paths walk identity.candidate_pages now, so the
        guards cannot come apart again by editing one of them.
    """
    for r in candidate_pages(name, careers_url):
        # A login redirect usually lands on the vendor's host -- check the
        # final URL first, then fall through to HTML body.
        hit = scan_hit(r.url) or scan_hit(r.text)
        if hit and not foreign_board(name, *hit):
            return _scan_meta(*hit, r.url)
    return None


# ─── JS-rendered scan probe (fallback for SPA careers pages) ─────────────
#
# Many Fortune-500 careers pages (NetApp, Cisco, Syneos, Precision
# BioSciences, WillowTree, etc.) are React/Angular SPAs: the board link
# is only inserted into the DOM after JS runs, so the static probe_scan
# above can't see it.
#
# JsScanProbe launches a single headless Playwright browser, reuses
# it across every candidate in a discover() run (browser startup is
# ~2-3s — not something we want to pay per candidate), and degrades
# cleanly when Playwright isn't installed. Use it as a context manager.

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from src.config import BROWSER_UA

# Wall-clock cap on one name's scrape. candidate_urls yields up to 12 pages
# and each can spend 20s in goto plus 6s waiting for networkidle, so one
# name could hold a browser for five minutes: discover-local 2026-09-22 sat
# 338s with no output inside the JS pass.
JS_PROBE_BUDGET_S = 60


class JsScanProbe:
    """
    Lazy-launched headless Playwright wrapper for JS-rendered careers
    pages, read for a SCANNED platform's board. Amortizes browser startup
    across many candidates.

    Usage:
        with JsScanProbe() as js:
            meta, outcome = js.probe("NetApp", careers_url="")

    If Playwright isn't installed or the browser fails to launch, the
    failure is reported once per process (_report_js_disabled) and every
    later probe() returns (None, "no browser").
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
        # requests and block on .result(), so the static probe_scan
        # paths stay fully parallel while the JS fallback is serialized
        # onto a single browser thread.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="scan-js",
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
        # Held locally: a launch that outlives the budget finishes after
        # _recycle swapped in a fresh stack, and must not hand its page
        # (bound to this abandoned thread) to the new one.
        stack = self._stack
        try:
            pw = stack.enter_context(sync_playwright())
            browser, _channel = launch_chromium(pw, headless=True)
            stack.callback(browser.close)
            context = browser.new_context(
                user_agent=BROWSER_UA,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
            )
            stack.callback(context.close)
            page = context.new_page()
        except Exception as e:
            _report_js_disabled(f"browser launch failed: {_js_launch_hint(e)}")
            self._enabled = False
            stack.close()
            return None
        if stack is self._stack:
            self._page = page
            self._launched = True
        _clear_js_disabled()
        return page

    @staticmethod
    def _scan(page, url: str):
        """
        Navigate + wait for JS, returning `scan_hit`'s (ats, handle) or
        None. Has three short-circuits so we don't pay the full
        networkidle wait on obvious non-matches:
          1. Did the URL redirect straight to the vendor's host?
          2. Is the board link in the initial server-rendered HTML?
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
        if (hit := scan_hit(cur)):
            return hit
        try:
            html = page.content()
        except Exception:
            html = ""
        if (hit := scan_hit(html)):
            return hit
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
        return scan_hit(cur) or scan_hit(html)

    # ── public API ───────────────────────────────────────────────────────

    def _probe_impl(self, name, careers_url, deadline):
        """Runs entirely on the browser-owning thread."""
        page = self._ensure_page()
        if page is None:
            return None, "no browser"
        for url in candidate_urls(name, careers_url):
            if time.monotonic() > deadline:
                # The caller gave up and recycled; stop loading pages so
                # this abandoned thread reaches its queued close.
                return None, "budget exceeded"
            hit = self._scan(page, url)
            if not hit or foreign_board(name, *hit):
                continue
            try:
                source = page.url
            except Exception:
                source = url
            meta = _scan_meta(*hit, source)
            return meta, "hit" if meta["validated"] else "not validated"
        return None, "no board link"

    def probe(self, name: str, careers_url: str = ""):
        """
        (meta, outcome): meta is probe_scan()'s shape or None; outcome
        is "hit", "not validated", "no board link", "no browser",
        "budget exceeded" or "errored: <exception>".

        Thread-safe: every Playwright call is dispatched onto the single
        browser-owning worker thread and the caller blocks on .result().
        Workers calling probe() concurrently queue behind each other,
        but their static probe_scan() work keeps running in parallel.
        """
        if not self._enabled:
            return None, "no browser"
        t0 = time.monotonic()
        fut = self._executor.submit(
            self._probe_impl, name, careers_url, t0 + JS_PROBE_BUDGET_S,
        )
        try:
            meta, outcome = fut.result(timeout=JS_PROBE_BUDGET_S)
        except Exception as e:
            meta = None
            if fut.done():
                # A browser-thread crash shouldn't poison the rest of discovery.
                print(f"    [js] probe for {name!r} errored: {e}")
                outcome = f"errored: {e}"
            else:
                self._recycle()
                outcome = "budget exceeded"
        _log.debug("js probe %s: %s in %.1fs", name, outcome,
                   time.monotonic() - t0)
        return meta, outcome

    def _recycle(self):
        """Abandon a browser thread stuck past the budget; start clean.

        A hung Playwright call cannot be interrupted from here, and the
        browser must be closed on the thread that built it, so the close
        queues behind the hung call and a fresh thread takes the next name.
        """
        old, stack = self._executor, self._stack
        old.submit(stack.close)
        old.shutdown(wait=False)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="scan-js",
        )
        self._stack = ExitStack()
        self._page = None
        self._launched = False

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


class JsScanProbePool:
    """K headless browsers running JS scan scrapes in parallel.

    A single JsScanProbe is single-threaded by necessity: Playwright's
    sync API pins its greenlet to one thread, so one instance serializes
    every scrape onto one browser. But nothing stops running SEVERAL
    instances at once: each owns its own Playwright + browser + thread, so
    K of them give K-way parallel scraping. Discovery workers that need the
    JS fallback borrow a free browser from the pool (blocking only when all
    K are busy) and hand it back when their scrape finishes.
    """

    def __init__(self, size):
        self.size = max(1, int(size))
        self._probes = [JsScanProbe() for _ in range(self.size)]
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


def _nc_count(ats, slug):
    """Postings on a board that are in your [locality] (`Board.local_count`):
    the count that rejects a slug guess landing on somebody else's board.
    `slug` is a resolver hit's."""
    return board_for(ats).local_count(_handle(ats, slug), NC_RE)


def probe_company(name, scan=True):
    """
    Probe every platform whose spec sets ``guess`` (fast) then, only if
    ``scan``, the SCANNED platforms (probe_scan, the slow careers-page
    fallback), then VERIFY the board has NC-area jobs (kills false-positive
    slug collisions and enforces local relevance).
    Returns a hit dict with an ``nc`` count, or None.
    """
    hit = None
    for slug in slug_guesses(name):
        for ats in (b.name for b in BOARDS.values() if b.spec.guess):
            ok, count = board_for(ats).probe(slug)
            if ok:
                hit = {"name": name, "ats": ats, "slug": slug,
                       "count": count, "nc": _nc_count(ats, slug)}
                break
        if hit:
            break
    if not hit and scan:
        s = probe_scan(name)
        if s and s["validated"]:
            hit = {"name": name, "ats": s["ats"], "slug": s["slug"],
                   "count": s["count"], "nc": _nc_count(s["ats"], s["slug"])}
    return hit
