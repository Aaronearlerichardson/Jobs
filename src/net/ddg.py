"""The one DuckDuckGo text-search client.

DDG is the crawl's single biggest time sink: a plain `DDGS().text(q)` has no
wall-clock bound, so when DDG rate-limits (frequent), the library's internal
retry/backoff blocks for many minutes yielding nothing -- profiled at ~1271s
of a 1726s --local run. Every caller (the local-sourcing resolvers, the ATS
dork sweep, the websearch fetcher) goes through `search`, which has:

  * one disk cache (7-day TTL) so repeat runs -- and repeat queries within a
    run -- return instantly instead of re-hitting DDG; only genuine non-empty
    hits are cached, so a throttled/empty query is retried next run;
  * one wall-clock budget per query: the library runs on a daemon thread
    of its own, the caller stops waiting for it at the deadline
    (asyncio.timeout), and it starts no retry once no one waits, so one
    throttled query gives up after ~budget seconds instead of stalling
    the whole crawl;
  * one retry policy: DDG surfaces its throttling as an exception ("No
    results found."/"Ratelimit"), and a fresh session after a pause recovers
    far more than a single try.

Notes:
    Three copies of this used to exist, each with one of the three guards:
    src.discovery.local_sourcing.ddg_text (cache + budget),
    src.discovery.dork._ddg_text (retries + paging + the frozen-build engine
    fix) and src.ats.feeds.websearch._ddg_search (none).
"""

import asyncio
import contextvars
import logging
import threading
import time

from src import config, runstate
from src.net.util import hashed_cache_path, json_cache_get, json_cache_put

# File-only diagnostics (session log DEBUG channel -- never printed).
_log = logging.getLogger("discovery")

CACHE_DIR = config.DATA_DIR / ".cache" / "ddg"
CACHE_TTL = 7 * 24 * 3600       # seconds
WALL_BUDGET = 25.0              # hard per-query wall-clock cap (seconds)
RETRIES = 2
RETRY_PAUSE = 2.5               # seconds, scaled by the attempt number

# ddgs (via primp) resolves hostnames with its OWN resolver rather than the
# OS one, and on a multi-adapter machine (VPN up, Wi-Fi also up) it can pick
# a server that answers REFUSED for every name while the OS resolver works
# fine. First sighting switches ddgs's client to config.SEARCH_DNS_FALLBACK
# (primp accepts a `dns_resolver` list that ddgs does not expose) and retries;
# if that is refused too, or no fallback is configured, the breaker trips
# and later queries return [] at once instead of burning RETRIES x
# RETRY_PAUSE per name. The trip is timed, not permanent: the 2026-09-22
# reresolve lost 24 of its 45 misses to a host DNS hiccup that had long
# cleared, because the breaker stayed shut for the rest of the run. After
# each window one query goes through as a probe; a refused probe doubles
# the window (capped). The breaker is the run's; the override patches the
# library, so it is the process's, until `reset_resolver()` drops it.
RESOLVER_WINDOW = 90.0          # seconds of skipping after the first trip
RESOLVER_WINDOW_CAP = 600.0
_RESOLVER_OVERRIDE = None       # (module, original Client) while installed


class _Searches:
    """What a run's searches share: the resolver breaker, and whether the
    missing package was announced. Their threads share it too, hence the
    lock."""

    def __init__(self):
        self.down_until = 0.0       # time.monotonic() deadline of the window
        self.backoff = 0.0          # current window; 0 = breaker closed
        self.probing = False        # a probe is in flight; others keep skipping
        self.missing_announced = False
        self.lock = threading.Lock()


_SEARCHES = runstate.per_run(_Searches)


# ─── Disk cache ──────────────────────────────────────────────────────────

def _cache_path(key):
    return hashed_cache_path(CACHE_DIR, key)


def cache_get(key):
    """The cached JSON value for `key`, or None when absent or older than
    CACHE_TTL. Also used by the LLM name brainstorm, which rides the same
    TTL."""
    return json_cache_get(_cache_path(key), CACHE_TTL)


def cache_put(key, value):
    """Best-effort write; a cache failure never fails the search."""
    json_cache_put(_cache_path(key), value)


# ─── The ddgs package ────────────────────────────────────────────────────

def _ddgs_class():
    """The DDGS class from whichever package name is installed, or None
    (announced once per run)."""
    try:
        from ddgs import DDGS                 # current name (2024+)
        return DDGS
    except ImportError:
        pass
    try:
        from duckduckgo_search import DDGS    # legacy name
        return DDGS
    except ImportError:
        searches = _SEARCHES()
        if not searches.missing_announced:
            searches.missing_announced = True
            print("    [!] ddgs not installed. Run: pip install ddgs")
        return None


# ddgs builds its engine registry by WALKING ITS OWN PACKAGE DIRECTORY
# (pkgutil.iter_modules in ddgs/engines/__init__.py). A compiled build has no
# directory to walk, so the registry comes up empty and every search dies on
# `ENGINES["text"]` -- a bare KeyError('text'), raised before ddgs's own backend
# error handling can run, so the whole search silently returns 0 results.
# Fallback list only -- a new ddgs release can add an engine this misses, which
# costs that one backend rather than the whole search.
_DDGS_ENGINE_MODULES = (
    "annasarchive", "bing", "bing_images", "bing_news", "brave", "duckduckgo",
    "duckduckgo_images", "duckduckgo_news", "duckduckgo_videos", "google",
    "grokipedia", "mojeek", "startpage", "wikipedia", "yahoo", "yahoo_news",
    "yandex",
)


def _ensure_ddgs_engines():
    """Re-register ddgs's search engines when its own discovery came up empty.
    No-op on a normal source run."""
    import importlib
    import inspect
    try:
        from ddgs.base import BaseSearchEngine
        from ddgs.engines import ENGINES
    except Exception:
        return
    if ENGINES.get("text"):
        return
    for modname in _DDGS_ENGINE_MODULES:
        try:
            module = importlib.import_module(f"ddgs.engines.{modname}")
        except Exception:
            continue
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (not issubclass(cls, BaseSearchEngine) or cls is BaseSearchEngine
                    or cls.__name__.startswith("Base")
                    or getattr(cls, "disabled", True)):
                continue
            name, category = getattr(cls, "name", None), getattr(cls, "category", None)
            if isinstance(name, str) and isinstance(category, str):
                ENGINES.setdefault(category, {})[name] = cls


# ─── Search ──────────────────────────────────────────────────────────────

def _resolver_failure(exc):
    """True when `exc` is the library's own resolver failing, as opposed
    to a throttle or an engine error.

    >>> _resolver_failure(Exception("ConnectError: dns error > DNS error: "
    ...                             "error response: Query Refused"))
    True
    >>> _resolver_failure(Exception("No results found."))
    False
    """
    msg = str(exc).lower()
    return any(m in msg for m in ("dns error", "query refused"))


def _http_client_module():
    """ddgs's HTTP-client module, where `primp.Client` is looked up each
    time a client is built (None when ddgs is not installed)."""
    try:
        import ddgs.http_client as hc
        return hc
    except Exception:
        return None


def _install_resolver(query):
    """Route ddgs's name resolution through config.SEARCH_DNS_FALLBACK by
    wrapping the `primp.Client` factory it builds its client from. Returns
    True when the override was installed just now (the caller retries),
    False when there is nothing to fall back to or it is already in place."""
    global _RESOLVER_OVERRIDE
    servers = [s for s in getattr(config, "SEARCH_DNS_FALLBACK", ()) if s]
    hc = _http_client_module()
    if _RESOLVER_OVERRIDE is not None or not servers or hc is None:
        return False
    orig = hc.primp.Client

    def client(*a, **kw):
        kw.setdefault("dns_resolver", list(servers))
        return orig(*a, **kw)

    hc.primp.Client = client
    _RESOLVER_OVERRIDE = (hc, orig)
    print(f"  [!] web search: the search library's resolver was refused on "
          f"{query[:40]!r}; retrying through {', '.join(servers)} for the "
          f"rest of this run (a VPN plus a second connected adapter is the "
          f"usual cause).")
    return True


def _trip_resolver(query, exc):
    s = _SEARCHES()
    with s.lock:
        prev = s.backoff
        if prev and not s.probing:
            return      # a query already in flight when the breaker tripped
        s.backoff = min(prev * 2, RESOLVER_WINDOW_CAP) if prev else RESOLVER_WINDOW
        s.down_until = time.monotonic() + s.backoff
        s.probing = False
    if prev:
        print(f"  [!] web search still unreachable after {prev:.0f}s, "
              f"backing off {s.backoff:.0f}s")
        return
    # Which path failed matters: on 2026-09-22 the fallback servers were
    # installed and refused too, which points at the host, not ddgs.
    via = ("the SEARCH_DNS_FALLBACK override ("
           + ", ".join(s for s in config.SEARCH_DNS_FALLBACK if s) + ")"
           if _RESOLVER_OVERRIDE is not None else "its own default resolver")
    print(f"  [!] web search unreachable: the search library's resolver "
          f"was refused via {via} ({type(exc).__name__} on {query[:40]!r}). "
          f"Skipping web search for {s.backoff:.0f}s, then probing.")


def _resolver_gate():
    """None (skip) while the breaker window is open, True for the one caller
    that becomes the probe after it expires, else False. The probe holds
    the rest off until it reports."""
    s = _SEARCHES()
    with s.lock:
        if not s.backoff:
            return False
        if s.probing or time.monotonic() < s.down_until:
            return None
        s.probing = True
        return True


def _probe_passed():
    """The probe got past name resolution (a hit, an empty or a throttle all
    count): close the breaker."""
    s = _SEARCHES()
    with s.lock:
        if not s.probing:
            return      # the probe was refused and re-tripped
        s.down_until = s.backoff = 0.0
        s.probing = False
    print("  web search resolver recovered")


def reset_resolver():
    """Drop the DNS override (tests, or a long-lived process after the
    network changed); each run's breaker starts closed."""
    global _RESOLVER_OVERRIDE
    if _RESOLVER_OVERRIDE is not None:
        hc, orig = _RESOLVER_OVERRIDE
        hc.primp.Client = orig
        _RESOLVER_OVERRIDE = None


def _query(DDGS, query, max_results, page, deadline, retries, stop):
    """One query with retry/backoff, on the search's own thread. Ends by
    itself about when the caller stops waiting: each attempt's timeout is
    at most the time left before `deadline` (time.monotonic()), and a retry
    that would start after it, or once `stop` is set, raises TimeoutError
    instead."""
    kwargs = {"max_results": max_results}
    if page != 1:
        # Forwarded by ddgs to the engine; the lever for getting past DDG's
        # default top-`max_results` ceiling.
        kwargs["page"] = page
    for attempt in range(retries + 1):
        try:
            left = round(deadline - time.monotonic())
            with DDGS(timeout=max(1, min(10, left))) as ddg:
                return list(ddg.text(query, **kwargs))
        except Exception as e:
            if _resolver_failure(e):
                if _install_resolver(query):
                    return _query(DDGS, query, max_results, page, deadline,
                                  retries, stop)
                _trip_resolver(query, e)
                return []
            if attempt < retries:
                pause = RETRY_PAUSE * (attempt + 1)
                if time.monotonic() + pause >= deadline or stop.wait(pause):
                    raise TimeoutError("no one waits for a retry") from e
                continue
            # "No results found." is DDG's way of returning an empty page
            # (and sometimes a disguised throttle -- hence the retries above);
            # once the retries are spent it's an expected empty.
            if "no results" in str(e).lower():
                return []
            # Type included: a bare KeyError prints as just its key ('text'),
            # which reads like a parsing quirk rather than a dead registry.
            print(f"  [!] ddg {query[:48]}...: {type(e).__name__}: {e}")
    return []


def _on_own_thread(loop, fn):
    """(future, stop): fn(stop) run on a daemon thread of its own, in a
    copy of the caller's context (its run), its result or exception set on
    `future` (of `loop`); the caller sets `stop`, a threading.Event, once
    it no longer waits.

    Notes:
        Was asyncio.to_thread, whose default executor queued a search
        behind other work on the search's own budget, and held a Ctrl+C'd
        exit about 23 s.
    """
    future, stop = loop.create_future(), threading.Event()

    def settle(ok, value):
        if not future.done():            # cancelled: no one waits
            (future.set_result if ok else future.set_exception)(value)

    def body():
        try:
            outcome = True, fn(stop)
        except Exception as e:
            outcome = False, e
        try:
            loop.call_soon_threadsafe(settle, *outcome)
        except RuntimeError:             # the loop has closed (exit)
            pass

    threading.Thread(target=body, daemon=True,
                     context=contextvars.copy_context()).start()
    return future, stop


async def search(query, max_results=10, page=1, budget=WALL_BUDGET,
                 retries=RETRIES):
    """Bounded, cached, retried DDG text search. Returns a list of result
    dicts (each with 'href'/'title'/...), or [] on miss, timeout or missing
    package -- every caller already tolerates an empty list. The disk
    cache is read and written off the loop."""
    key = f"{query}||{max_results}" + (f"||page={page}" if page != 1 else "")
    cached = await asyncio.to_thread(cache_get, key)
    if cached is not None:
        _log.debug("ddg cache hit (%d result(s)): %s", len(cached), query)
        return cached
    probe = _resolver_gate()
    if probe is None:
        _log.debug("ddg skipped, resolver breaker tripped: %s", query)
        return []
    deadline = time.monotonic() + budget

    def run(stop):
        # The library blocks, and its first import is slow: off the loop.
        DDGS = _ddgs_class()
        if DDGS is None:
            return None
        _ensure_ddgs_engines()
        try:
            return _query(DDGS, query, max_results, page, deadline, retries,
                          stop)
        finally:
            if probe:
                _probe_passed()

    done, stop = _on_own_thread(asyncio.get_running_loop(), run)
    try:
        async with asyncio.timeout(budget):
            out = await done
    except TimeoutError:
        _log.debug("ddg timed out after %.0fs: %s", budget, query)
        return []
    finally:
        stop.set()
    if out is None:                      # no ddgs package (announced)
        return []
    _log.debug("ddg live query, %d result(s): %s", len(out), query)
    if out:                              # cache only genuine hits
        await asyncio.to_thread(cache_put, key, out)
    return out


async def search_urls(query, max_results=10, page=1):
    """The result URLs of `search`, in order, skipping results without one."""
    return [u for r in await search(query, max_results, page=page)
            if (u := (r.get("href") or r.get("url")))]
