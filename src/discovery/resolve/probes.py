"""ATS slug probes — cheap HEAD/GET checks to confirm a slug is real."""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Protocol, Self, cast

from src import config, runstate
from src.ats import coords
from src.ats.board import BOARDS, board_for
from src.ats.board.engine import Board
from src.ats.signatures import detect, pack
from src.match.locality import NC_RE
from src.match.names import slug_guesses
from src.rows import BoardHit, Slug
from .domain import seed_urls
from .fetchpool import _drop_unresolvable, candidate_urls
from .identity import candidate_pages, foreign_board


class _Context(Protocol):
    """The slice of a Playwright browser context the pool uses."""
    async def new_page(self) -> _Page: ...
    async def close(self) -> None: ...


class _Page(Protocol):
    """The slice of a Playwright page the pool uses."""
    @property
    def url(self) -> str: ...
    @property
    def context(self) -> _Context: ...
    async def goto(self, url: str, *, wait_until: str, timeout: int) -> object: ...
    async def content(self) -> str: ...
    async def wait_for_load_state(self, state: str, *, timeout: int) -> None: ...


class _Browser(Protocol):
    """The slice of a Playwright browser the pool uses."""
    async def new_context(self, *, user_agent: str, viewport: dict[str, int],
                          locale: str, timezone_id: str) -> _Context: ...
    async def close(self) -> None: ...


class _Chromium(Protocol):
    async def launch(self, **opts: object) -> _Browser: ...


class _Playwright(Protocol):
    @property
    def chromium(self) -> _Chromium: ...


# File-only diagnostics (session log DEBUG channel — never printed).
_log = logging.getLogger("src.discovery.resolve.probes")

# Whether the headless browser is usable is a RUN fact, not a per-pool
# one: a run can start more than one JsScanProbePool, and Playwright's
# launch error embeds a ten-line ASCII banner.
_JS_NOTICES: Callable[[], set[str]] = runstate.per_run(set)


def _js_notice_once(key: str, message: str) -> bool:
    """Print a `[js]` notice the first time `key` comes up. True if printed."""
    notices = _JS_NOTICES()
    if key in notices:
        return False
    notices.add(key)
    print(f"    [js] {message}")
    return True


def _js_launch_hint(exc: Exception) -> str:
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


def _report_js_disabled(detail: str) -> bool:
    """Say the JS fallback is off, once per run. True if we reported."""
    return _js_notice_once("disabled", f"{detail}; JS scan probe disabled")


async def launch_chromium(pw: _Playwright, **kwargs: bool) -> tuple[_Browser, str | None]:
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
            opts: dict[str, object] = dict(kwargs)
            if channel:
                opts["channel"] = channel
            browser = await pw.chromium.launch(**opts)
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
    raise cast(Exception, first_error)


# ─── Scanned platforms (a handle no name guess reaches) ──────────────────
#
# A platform whose spec sets `discovery.scan` names its board with a handle
# the company name cannot produce (Workday's tenant, pod and site:
# redhat.wd5.myworkdayjobs.com/Jobs_External), so probe_scan reads it off
# the company's careers page(s) (`scan_hit`), then counts the board's
# listing (`Board.alive`). probe_company calls it as its last step.

#: The platforms whose spec sets `discovery.scan`, in spec order.
SCANNED = tuple(b.name for b in BOARDS.values() if b.fetchable and b.spec.discovery.scan)


def slug_keyed(board: Board) -> bool:
    """Whether `board`'s handle is the parts a detection reads (a slug,
    Workday's triple) rather than a careers URL, which a URL on the vendor's
    host does not name."""
    return "careers_url" not in board.spec.handle.columns


async def confirm(ats: str, slug: Slug, careers_url: str | None = None) -> int | None:
    """A live posting count for detected coordinates, or None: the board's
    probe on the handle they name as store columns (`coords.columns`), so
    a careers_url-keyed board is probed at its careers URL."""
    b = board_for(ats)
    if not b:
        return None
    handle = b.handle(coords.columns(ats, slug, careers_url))
    if not handle:
        return None
    ok, count = await b.probe(handle)
    return count if ok else None


def scan_hit(text: str | None) -> tuple[str, Slug] | None:
    """(ats, handle) of the first board of a SCANNED platform `text` names
    (`signatures.detect` restricted to each), or None."""
    for ats in SCANNED:
        hit = detect(text or "", only=ats)
        if hit:
            return ats, hit[2]
    return None


def _handle(ats: str, slug: Slug) -> str | None:
    """The engine handle for a resolver hit's slug (a tuple where the
    handle has several parts); None when a part is empty."""
    return cast(Board, board_for(ats)).handle(coords.columns(ats, slug))


async def _scan_meta(ats: str, handle: Slug, source_url: str) -> BoardHit:
    """probe_scan's answer for `ats`'s board `handle`, found at
    `source_url`: counted through its listing, `validated` when that
    answered. `careers_url` is the board's as `pack` reads it off the page,
    which a careers-URL-keyed board's handle needs."""
    curl = pack(ats, handle, source_url)["careers_url"]
    h = cast(Board, board_for(ats)).handle(coords.columns(ats, handle, curl))
    ok, n = await cast(Board, board_for(ats)).alive(h) if h else (False, 0)
    return {"ats": ats, "slug": handle, "count": n if ok else 0,
            "validated": ok, "source_url": source_url, "careers_url": curl}


def page_hit(text: str | None, url: str = "") -> tuple[str, Slug] | None:
    """(ats, handle) of the first fetchable board `text` or `url` names, any
    platform (`signatures.detect`, as the careers-page sniff reads a page)."""
    hit = detect(text or "", url, leads=False)
    return (hit[1], hit[2]) if hit else None


async def probe_scan(name: str, careers_url: str = "") -> BoardHit | None:
    """
    The board of a SCANNED platform that `name`'s careers pages name
    (identity.candidate_pages, fetched through the per-run memo, each read
    off the loop), counted through its listing.

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
    for r in await candidate_pages(name, careers_url):
        # A login redirect usually lands on the vendor's host -- check the
        # final URL first, then fall through to HTML body.
        hit = scan_hit(r.url) or await asyncio.to_thread(lambda: scan_hit(r.text))
        if hit and not await foreign_board(name, *hit):
            return await _scan_meta(*hit, r.url)
    return None


# ─── JS-rendered scan probe (fallback for SPA careers pages) ─────────────
#
# Many Fortune-500 careers pages (NetApp, Cisco, Syneos, Precision
# BioSciences, WillowTree, etc.) are React/Angular SPAs: the board link
# is only inserted into the DOM after JS runs, so the static probe_scan
# and sniff cannot see it. The rendered page is read for ANY fetchable
# platform (`page_hit`): read for Workday alone, 15 of 17 majors' Phenom,
# SuccessFactors and Eightfold boards came back "no board link" (2026-10-08).


class JsScanProbePool:
    """Up to `size` JS scan scrapes at once, in one lazily launched
    headless browser.

    Each scrape takes a free page, which lives in a browser context of its
    own and is kept for later scrapes; a caller waits while all `size`
    slots are busy. aclose() shuts the browser down.

    Usage:
        async with JsScanProbePool(4) as js:
            meta, outcome = await js.probe("NetApp", careers_url="")

    If Playwright isn't installed or the browser fails to launch, the
    failure is reported once per run (_report_js_disabled) and every
    later probe() returns (None, "no browser").

    Notes:
        Was `size` whole browsers, each pinned to a thread of its own
        because sync Playwright binds to the thread that starts it. Over
        200 local careers pages 4 wide, pages in one browser answered the
        same, in the same wall time, with 0.9 GB of private memory
        against 1.9-2.4 GB. A context per page, not per scrape: a fresh
        one costs about 3.5x the CPU.
    """

    def __init__(self, size: int) -> None:
        self.size = max(1, size)
        self._slots = asyncio.Semaphore(self.size)
        self._launching = asyncio.Lock()
        self._stack = AsyncExitStack()
        self._browser: _Browser | None = None
        self._idle: list[_Page] = []          # pages free for the next scrape
        self._enabled = True     # False after a launch failure, or close()

    async def _launch(self) -> None:
        """Start Playwright and the browser on self._stack, or report why
        not and disable the pool."""
        try:
            # Off the loop, as the import takes over 100 ms; and only here,
            # so a process that never probes (the harvester) never loads it.
            api = await asyncio.to_thread(importlib.import_module,
                                          "playwright.async_api")
        except ImportError:
            _report_js_disabled("playwright not installed")
            self._enabled = False
            return
        try:
            pw = await self._stack.enter_async_context(api.async_playwright())
            browser, _channel = await launch_chromium(pw, headless=True)
            self._stack.push_async_callback(browser.close)
        except Exception as e:
            _report_js_disabled(f"browser launch failed: {_js_launch_hint(e)}")
            self._enabled = False
            await self._stack.aclose()
            return
        self._browser = browser
        # Re-armed, so a later failure in the same run is reported.
        _JS_NOTICES().discard("disabled")

    async def _page(self) -> _Page | None:
        """A free page, made on first need; None when there is no browser."""
        if self._idle:
            return self._idle.pop()
        async with self._launching:
            if self._browser is None and self._enabled:
                await self._launch()
        if self._browser is None:
            return None
        context = await self._browser.new_context(
            user_agent=config.BROWSER_UA,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        return await context.new_page()

    @staticmethod
    async def _scan(page: _Page, url: str) -> tuple[str, Slug] | None:
        """
        Navigate + wait for JS, returning `page_hit`'s (ats, handle) or
        None. Has three short-circuits so we don't pay the full
        networkidle wait on obvious non-matches:
          1. Did the URL redirect straight to the vendor's host?
          2. Is the board link in the initial server-rendered HTML?
          3. After JS settles (networkidle, capped at 6s), try again.
        """
        # `_launch` has already imported it (off the loop), so this is a lookup.
        from playwright.async_api import Error as PlaywrightError
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except PlaywrightError as e:
            msg = str(e)
            if ("interrupted by another navigation" not in msg
                    and "Navigation timeout" not in msg):
                _log.debug("js scan %s: goto failed: %s", url, e)
                return None
        if (hit := page_hit("", page.url)):
            return hit
        try:
            html = await page.content()
        except PlaywrightError as e:
            _log.debug("js scan %s: content failed: %s", url, e)
            html = ""
        if (hit := await asyncio.to_thread(page_hit, html)):
            return hit
        # Wait for JS-deferred content (iframes, ajax-injected links).
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except PlaywrightError as e:
            _log.debug("js scan %s: networkidle wait: %s", url, e)
        try:
            cur = page.url
            html = await page.content()
        except PlaywrightError as e:
            _log.debug("js scan %s: re-read failed: %s", url, e)
            return None
        return page_hit("", cur) or await asyncio.to_thread(page_hit, html)

    @classmethod
    async def _scrape(cls, page: _Page, name: str,
                      careers_url: str) -> tuple[BoardHit | None, str]:
        """probe's answer from `page`, once it has one. A guessed host that
        does not resolve, or refused the static fetch, is not navigated:
        each such goto spent seconds to minutes of the name's budget.
        The official domain's pages (`domain.seed_urls`) go first: the
        name guesses alone sent Eli Lilly to elililly.com and eli.com.
        """
        seeds = await seed_urls(name, careers_url)
        urls = dict.fromkeys([*seeds, *(s + "careers" for s in seeds[:1]),
                              *candidate_urls(name, careers_url)])
        for url in await _drop_unresolvable(list(urls)):
            hit = await cls._scan(page, url)
            if not hit or await foreign_board(name, *hit):
                continue
            meta = await _scan_meta(*hit, page.url)
            return meta, "hit" if meta["validated"] else "not validated"
        return None, "no board link"

    async def probe(self, name: str, careers_url: str = "") -> tuple[BoardHit | None, str]:
        """
        (meta, outcome): meta is probe_scan()'s shape or None; outcome
        is "hit", "not validated", "no board link", "no browser",
        "budget exceeded" or "errored: <exception>".

        One name's scrape, a browser launch included, gets
        config.JS_PROBE_BUDGET_S; the wait for a free page does not count.
        A page cut off (the budget, or a cancel) is closed rather than
        handed to the next name.
        """
        if not self._enabled:
            return None, "no browser"
        await self._slots.acquire()
        t0 = time.monotonic()
        budget = asyncio.timeout(config.JS_PROBE_BUDGET_S)
        page, keep = None, False
        try:
            async with budget:
                page = await self._page()
                meta, outcome = (await self._scrape(page, name, careers_url)
                                 if page else (None, "no browser"))
            keep = True
        except Exception as e:
            meta = None
            if budget.expired():
                outcome = "budget exceeded"
            else:
                # A browser crash shouldn't poison the rest of discovery.
                print(f"    [js] probe for {name!r} errored: {e}")
                outcome, keep = f"errored: {e}", True
        finally:
            self._slots.release()
            if page is not None and keep:
                self._idle.append(page)
            elif page is not None:
                try:
                    await page.context.close()
                except Exception as e:
                    _log.debug("js page close errored: %s", e)
        _log.debug("js probe %s: %s in %.1fs", name, outcome,
                   time.monotonic() - t0)
        return meta, outcome

    @property
    def launched(self) -> bool:
        """True once the browser has actually started (for logging)."""
        return self._browser is not None

    async def aclose(self) -> None:
        """Shut the browser down, after any launch under way; a later
        probe answers "no browser"."""
        async with self._launching:
            self._enabled, self._browser = False, None
            self._idle.clear()
            try:
                await self._stack.aclose()
            except Exception as e:
                print(f"    [js] browser close errored: {e}")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_a: object) -> None:
        await self.aclose()


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


async def nc_count(ats: str, slug: Slug) -> int:
    """Postings on a board that are in your [locality] (`Board.local_count`):
    the count that rejects a slug guess landing on somebody else's board.
    `slug` is a resolver hit's."""
    h = _handle(ats, slug)
    return await cast(Board, board_for(ats)).local_count(h, NC_RE) if h else 0


async def probe_company(name: str, scan: bool = True) -> BoardHit | None:
    """
    Probe every platform whose spec sets ``guess`` (fast) then, only if
    ``scan``, the SCANNED platforms (probe_scan, the slow careers-page
    fallback), then VERIFY the board has NC-area jobs (kills false-positive
    slug collisions and enforces local relevance).
    Returns a hit dict with an ``nc`` count, or None.
    """
    hit: BoardHit | None = None
    for slug in slug_guesses(name):
        for ats in (b.name for b in BOARDS.values() if b.spec.guess):
            ok, count = await cast(Board, board_for(ats)).probe(slug)
            if ok:
                hit = {"name": name, "ats": ats, "slug": slug,
                       "count": count, "nc": await nc_count(ats, slug)}
                break
        if hit:
            break
    if not hit and scan:
        s = await probe_scan(name)
        if s and s["validated"]:
            hit = {"name": name, "ats": s["ats"], "slug": s["slug"],
                   "count": s["count"], "nc": await nc_count(s["ats"], s["slug"])}
    return hit
