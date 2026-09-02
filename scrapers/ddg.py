"""The one DuckDuckGo text-search client.

DDG is the crawl's single biggest time sink: a plain `DDGS().text(q)` has no
wall-clock bound, so when DDG rate-limits (frequent), the library's internal
retry/backoff blocks for many minutes yielding nothing -- profiled at ~1271s
of a 1726s --local run. Every caller (the local-sourcing resolvers, the ATS
dork sweep, the websearch fetcher) goes through `search`, which has:

  * one disk cache (7-day TTL) so repeat runs -- and repeat queries within a
    run -- return instantly instead of re-hitting DDG; only genuine non-empty
    hits are cached, so a throttled/empty query is retried next run;
  * one wall-clock budget per query, enforced with a worker thread and
    join(timeout), so one throttled query abandons after ~budget seconds
    instead of stalling the whole crawl;
  * one retry policy: DDG surfaces its throttling as an exception ("No
    results found."/"Ratelimit"), and a fresh session after a pause recovers
    far more than a single try.

Notes:
    Three copies of this used to exist, each with one of the three guards:
    discovery.local_sourcing.ddg_text (cache + budget),
    discovery.ats_dork._ddg_text (retries + paging + the frozen-build engine
    fix) and scrapers.fetchers.websearch._ddg_search (none).
"""

import hashlib
import json
import logging
import threading
import time

import config

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
# and every later query in the process returns [] at once instead of
# burning RETRIES x RETRY_PAUSE per name. `reset_resolver()` re-arms both.
_RESOLVER_DOWN = False
_RESOLVER_OVERRIDE = None       # (module, original Client) while installed
_RESOLVER_MARKERS = ("dns error", "query refused")


# ─── Disk cache ──────────────────────────────────────────────────────────

def _cache_path(key):
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.json"


def cache_get(key):
    """The cached JSON value for `key`, or None when absent or older than
    CACHE_TTL. Also used by the LLM name brainstorm, which rides the same
    TTL."""
    p = _cache_path(key)
    try:
        if time.time() - p.stat().st_mtime > CACHE_TTL:
            return None
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return None


def cache_put(key, value):
    """Best-effort write; a cache failure never fails the search."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(key).write_text(json.dumps(value), encoding="utf-8")
    except Exception:
        pass


# ─── The ddgs package ────────────────────────────────────────────────────

_MISSING_ANNOUNCED = False


def _ddgs_class():
    """The DDGS class from whichever package name is installed, or None
    (announced once per process)."""
    global _MISSING_ANNOUNCED
    try:
        from ddgs import DDGS                 # current name (2024+)
        return DDGS
    except ImportError:
        pass
    try:
        from duckduckgo_search import DDGS    # legacy name
        return DDGS
    except ImportError:
        if not _MISSING_ANNOUNCED:
            _MISSING_ANNOUNCED = True
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
    return any(m in msg for m in _RESOLVER_MARKERS)


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
    global _RESOLVER_DOWN
    if not _RESOLVER_DOWN:
        _RESOLVER_DOWN = True
        print(f"  [!] web search unreachable: the search library's resolver "
              f"was refused ({type(exc).__name__} on {query[:40]!r}). "
              f"Skipping web search for the rest of this run.")


def reset_resolver():
    """Re-arm the resolver breaker and drop the DNS override (tests, or a
    long-lived process after the network changed)."""
    global _RESOLVER_DOWN, _RESOLVER_OVERRIDE
    _RESOLVER_DOWN = False
    if _RESOLVER_OVERRIDE is not None:
        hc, orig = _RESOLVER_OVERRIDE
        hc.primp.Client = orig
        _RESOLVER_OVERRIDE = None


def _query(DDGS, query, max_results, page, budget, retries):
    """One query with retry/backoff. Runs on the worker thread; the caller
    may have stopped waiting for it by the time it returns."""
    kwargs = {"max_results": max_results}
    if page != 1:
        # Forwarded by ddgs to the engine; the lever for getting past DDG's
        # default top-`max_results` ceiling.
        kwargs["page"] = page
    for attempt in range(retries + 1):
        try:
            with DDGS(timeout=min(10, int(budget))) as ddg:
                return list(ddg.text(query, **kwargs))
        except Exception as e:
            if _resolver_failure(e):
                if _install_resolver(query):
                    return _query(DDGS, query, max_results, page, budget,
                                  retries)
                _trip_resolver(query, e)
                return []
            if attempt < retries:
                time.sleep(RETRY_PAUSE * (attempt + 1))
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


def search(query, max_results=10, page=1, budget=WALL_BUDGET, retries=RETRIES):
    """Bounded, cached, retried DDG text search. Returns a list of result
    dicts (each with 'href'/'title'/...), or [] on miss, timeout or missing
    package -- every caller already tolerates an empty list."""
    key = f"{query}||{max_results}" + (f"||page={page}" if page != 1 else "")
    cached = cache_get(key)
    if cached is not None:
        _log.debug("ddg cache hit (%d result(s)): %s", len(cached), query)
        return cached
    if _RESOLVER_DOWN:
        _log.debug("ddg skipped, resolver breaker tripped: %s", query)
        return []
    DDGS = _ddgs_class()
    if DDGS is None:
        return []
    _ensure_ddgs_engines()
    box = {}

    def _run():
        box["v"] = _query(DDGS, query, max_results, page, budget, retries)

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(budget)
    out = box.get("v") or []
    if th.is_alive():
        _log.debug("ddg timed out after %.0fs: %s", budget, query)
    else:
        _log.debug("ddg live query, %d result(s): %s", len(out), query)
    if out:                              # cache only genuine hits
        cache_put(key, out)
    return out


def search_urls(query, max_results=10, page=1):
    """The result URLs of `search`, in order, skipping results without one."""
    return [u for r in search(query, max_results, page=page)
            if (u := (r.get("href") or r.get("url")))]
