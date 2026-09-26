"""Concurrent work as tasks on the event loop.

Fetchers are network-bound and independent per source, so running them
together takes a ~30-source crawl from minutes (serial + sleeps) to
roughly the slowest single source. Per-source pacing stays inside each
fetcher (the host limiter, the page and detail delays); running them
together only removes the dead time *between* sources.

`fan_out` runs a coroutine function over items and yields what comes
back as it comes back; `fetch_all` is the crawl's source fan-out, its
results in input order so callers can process priority sources first and
keep dedupe deterministic regardless of completion order.
"""

import asyncio
import time

from src import config
from . import http
from .util import worker_count

# Network-I/O-bound, so this is a concurrency knob, not a CPU one: defaults
# to n_cpus-1, raise CRAWLER_WORKERS to push more concurrent source fetches.
DEFAULT_WORKERS = worker_count("crawler_workers")

# Stall watchdog: abandon the remaining work if NOTHING completes for this
# long. Generous on purpose - a normal resolution chains a handful of
# bounded fetches; only a genuinely wedged one exceeds this.
RESOLVE_STALL_S = 300.0


async def fan_out(items, fn, label="task", max_workers=DEFAULT_WORKERS,
                  with_item=False, on_error=None, budget_s=None,
                  on_abandon=None, stall_s=None):
    """Run the coroutine function `fn` over every item, at most
    `max_workers` at a time; yield what came back.

    Results arrive in COMPLETION order, so a consumer may write to the
    store between yields. Failures are reported and skipped, never raised:
    one dead board must not abandon the other two hundred.

    >>> async def tenfold(n):
    ...     return n * 10
    >>> async def results(gen):
    ...     return [x async for x in gen]
    >>> sorted(asyncio.run(results(fan_out([1, 2, 3], tenfold))))
    [10, 20, 30]

    `with_item=True` yields (item, result):

    >>> async def size(s):
    ...     return len(s)
    >>> sorted(asyncio.run(results(fan_out(["a", "bb"], size, with_item=True))))
    [('a', 1), ('bb', 2)]

    Each call counts its own fetch failures (net.http.fetch_failures):

    >>> async def count(n):
    ...     if n:
    ...         return http.fetch_failed("item 1", "timeout", indent=0)
    ...     await asyncio.sleep(0.05)
    ...     return http.fetch_failures()
    >>> asyncio.run(results(fan_out([0, 1], count)))
    [!] item 1: timeout
    [[], 0]

    `label` names the work in the failure line, and may be a callable when
    the item itself is the interesting part of the message; `on_error(item,
    exc)` replaces the reporting entirely.

    `budget_s` bounds the whole pass, and `stall_s` a stretch in which
    nothing completes (the watchdog for work that can WEDGE rather than
    fail: a company resolution chaining page fetches). Past either, each
    item not yet done is reported abandoned, passed to `on_abandon(item)`,
    cancelled and never yielded, so a consumer that writes only what it is
    handed records nothing for it:

    >>> async def slow(n):
    ...     await asyncio.sleep(n)
    ...     return n
    >>> asyncio.run(results(fan_out([0, 5], slow, str, budget_s=0.2)))
        [!] 5: past its 0.2s budget - abandoned
    [0]

    A consumer that leaves early (`break`, an exception, a cancel) cancels
    the calls still running; under contextlib.aclosing at once, otherwise
    when the generator is collected.

    Notes:
        Was a thread pool (fan_out, and drain for the stall watchdog)
        until phase 7 of the async migration: a running thread could only
        be abandoned, and nothing waited for it, so a wedged resolution
        held the web UI's one op slot for over an hour (2026-08-28).
    """
    items = list(items)
    if not items:
        return
    what = label if callable(label) else (lambda _item: label)
    slots = asyncio.Semaphore(max(1, max_workers))

    async def call(item):
        async with slots:
            http.reset_fetch_failures()     # this item's own count
            return await fn(item)

    loop = asyncio.get_running_loop()
    tasks = {loop.create_task(call(item)): item for item in items}
    end = None if budget_s is None else loop.time() + budget_s
    pending = set(tasks)
    try:
        while pending:
            wait_s = stall_s if end is None else max(0.0, end - loop.time())
            done, pending = await asyncio.wait(pending, timeout=wait_s,
                                               return_when=asyncio.FIRST_COMPLETED)
            if not done:
                why = (f"no progress in {stall_s:g}s" if end is None
                       else f"past its {budget_s:g}s budget")
                for t in pending:
                    print(f"    [!] {what(tasks[t])}: {why} - abandoned")
                    if on_abandon:
                        on_abandon(tasks[t])
                return
            for t in done:
                item = tasks[t]
                try:
                    result = t.result()
                except Exception as e:          # noqa: BLE001 - per item
                    if on_error:
                        on_error(item, e)
                    else:
                        print(f"    [!] {what(item)} error: {e}")
                    continue
                yield (item, result) if with_item else result
    finally:
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending)


async def fetch_all(sources, max_workers=DEFAULT_WORKERS, on_done=None,
                    budget_s=None):
    """Run every (name, platform, fetch) source concurrently: `fetch()` is
    the source's coroutine.

    Returns a list aligned with `sources`: each element is
    (jobs, error, snapshot). `error` is None on success; on failure `jobs`
    is [] and `snapshot` None. `snapshot` is net.http.snapshot_info() for
    that source's own fetch, so a caller can tell a complete board from an
    incomplete or capped one; each source counts in a task of its own:

    >>> async def bad():
    ...     return http.fetch_failed("bad", "timeout", indent=0)
    >>> async def ok():
    ...     return [1]
    >>> [(jobs, snap["incomplete"]) for jobs, _, snap in
    ...  asyncio.run(fetch_all([("bad", "x", bad), ("ok", "x", ok)]))]
    [!] bad: timeout
    [([], True), ([1], False)]

    A fetch that raises is the `error` arm:

    >>> async def boom():
    ...     raise OSError("refused")
    >>> asyncio.run(fetch_all([("dead", "x", boom)]))
    [([], OSError('refused'), None)]

    So is a source still unfinished when `budget_s` (default
    config.FETCH_BUDGET_S) runs out: it is reported abandoned, and no
    caller reconciles or closes anything from it.

    >>> async def hung():
    ...     await asyncio.sleep(5)
    >>> asyncio.run(fetch_all([("slow", "x", hung)], budget_s=0.2))
        [!] slow: past its 0.2s budget - abandoned
    [([], TimeoutError('past its 0.2s budget'), None)]

    `on_done(name, platform, jobs, error)` fires as each source completes
    (completion order), for progress output.
    """
    budget_s = config.FETCH_BUDGET_S if budget_s is None else budget_s
    results = [([], None, None)] * len(sources)

    def done(i, got):
        results[i] = got
        if on_done:
            on_done(sources[i][0], sources[i][1], got[0], got[1])

    async def accounted(i):
        jobs = await sources[i][2]() or []
        return jobs, http.snapshot_info()

    def abandoned(i):
        results[i] = ([], TimeoutError(f"past its {budget_s:g}s budget"), None)

    async for i, (jobs, snap) in fan_out(
            range(len(sources)), accounted, lambda i: sources[i][0], max_workers,
            with_item=True, on_error=lambda i, e: done(i, ([], e, None)),
            budget_s=budget_s, on_abandon=abandoned):
        done(i, (jobs, None, snap))
    return results


class SingleFlight:
    """A per-key memo that concurrent callers of one key fill ONCE.

    `await do(key, make, ttl)` returns the value kept for `key`, else
    `await make()`'s: a caller arriving while another awaits make() for the
    same key waits for it and reads the value it kept. A value is kept for
    `ttl` seconds (for good when None), and only when `keep(value)` holds;
    with nothing kept, each caller runs make() itself, one at a time.

    >>> calls = []
    >>> async def made():
    ...     return calls.append(1) or "v"
    >>> async def nothing():
    ...     calls.append(1)
    >>> memo = SingleFlight(keep=lambda v: v is not None)
    >>> async def twice(key, make):
    ...     return [await memo.do(key, make) for _ in "ab"]
    >>> asyncio.run(twice("k", made)), len(calls)
    (['v', 'v'], 1)
    >>> asyncio.run(twice("n", nothing)), len(calls)
    ([None, None], 3)

    `hold(key)` is the lock a maker of `key` holds, for a value kept
    somewhere else (the board engine's settled handle parts).
    """

    def __init__(self, keep=None):
        self._keep = keep
        self._memo = {}                 # key -> (expires or None, value)
        self._locks = {}

    def hold(self, key):
        """The asyncio.Lock one maker of `key` holds at a time."""
        return self._locks.setdefault(key, asyncio.Lock())

    def _kept(self, key):
        got = self._memo.get(key)
        return got if got and (got[0] is None or time.monotonic() < got[0]) else None

    async def do(self, key, make, ttl=None):
        """The value kept for `key`, else make()'s (see the class)."""
        got = self._kept(key)
        if got is None:
            async with self.hold(key):
                got = self._kept(key) or self._made(key, await make(), ttl)
        return got[1]

    def _made(self, key, value, ttl):
        """(expiry, value) for a value just made, kept when `keep` allows."""
        got = (None if ttl is None else time.monotonic() + ttl, value)
        if self._keep is None or self._keep(value):
            self._memo[key] = got
        return got
