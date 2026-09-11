"""Parallel source fetching.

Fetchers are network-bound and independent per source, so a small thread
pool takes a ~30-source crawl from minutes (serial + sleeps) to roughly
the slowest single source. Per-source rate limiting stays inside each
fetcher (their internal sleeps still apply); the pool only removes the
dead time *between* sources.

Results are returned in input order so callers can process priority
sources first and keep dedupe deterministic regardless of completion
"""

from concurrent.futures import (FIRST_COMPLETED, ThreadPoolExecutor,
                                as_completed, wait as fut_wait)

from .util import worker_count

# Network-I/O-bound, so this is a concurrency knob, not a CPU one: defaults
# to n_cpus-1, raise CRAWLER_WORKERS to push more concurrent source fetches.
DEFAULT_WORKERS = worker_count("CRAWLER_WORKERS")

# Stall watchdog: abandon a pool's remaining work if NOTHING completes for
# this long. Generous on purpose - a normal resolution chains a handful of
# bounded fetches; only a genuinely wedged one exceeds this.
RESOLVE_STALL_S = 300.0


def drain_or_abandon(ex, futs, consume, stalled):
    """Drain `futs` ({future: label}) through consume(future, label); if no
    future completes within RESOLVE_STALL_S, report each remaining label to
    stalled(label) instead and shut the executor down WITHOUT joining its
    threads. The one watchdog every discovery pool runs under.

    Notes:
        The `with ThreadPoolExecutor(...)` form joins every worker on exit,
        so one wedged resolution (fetch_company_nc on a sprawling "custom
        board" is bounded per request, not in total) used to hold the web
        UI's one-op-at-a-time slot until the app was restarted - 2026-08-28:
        an add-names run finished 59 of 60 names in 8 minutes, then hung
        >1h on the last. Behavior is enforced by tests/test_parsers.py::
        TestResolutionStallWatchdog.
    """
    pending = set(futs)
    while pending:
        done, pending = fut_wait(pending, timeout=RESOLVE_STALL_S,
                                 return_when=FIRST_COMPLETED)
        if not done:
            for fut in pending:
                n = futs[fut]
                print(f"    [!] {n}: no progress in {RESOLVE_STALL_S:.0f}s "
                      f"- abandoned")
                stalled(n)
            break
        for fut in done:
            consume(fut, futs[fut])
    ex.shutdown(wait=False, cancel_futures=True)


def fan_out(items, fn, label="task", max_workers=DEFAULT_WORKERS,
            with_item=False, on_error=None):
    """Run `fn` over every item in a thread pool; yield what came back.

    Results arrive in COMPLETION order, on the caller's thread, so a
    consumer may write to the store (or to any other single-threaded
    resource) between yields exactly as the hand-rolled version did.
    Failures are reported and skipped, never raised: one dead board must
    not abandon the other two hundred.

    >>> list(sorted(fan_out([1, 2, 3], lambda n: n * 10)))
    [10, 20, 30]

    `with_item=True` yields (item, result) for a consumer that needs the
    input back -- the `futs[fut]` lookup that every second call site had
    grown its own dict for:

    >>> sorted(fan_out(["a", "bb"], len, with_item=True))
    [('a', 1), ('bb', 2)]

    An empty input runs no pool at all (`max_workers=0` is an error, which
    is why half the call sites carried a `min(8, len(todo))` guard):

    >>> list(fan_out([], print))
    []

    `label` names the work in the failure line, and may be a callable when
    the item itself is the interesting part of the message; `on_error(item,
    exc)` replaces the reporting entirely.

    A consumer may `break` out: whatever has not started is cancelled and
    the pool is NOT joined, so a caller that has decided to stop (the deep
    verify's API breaker, say) returns at once rather than waiting out
    every queued call. `with ThreadPoolExecutor(...)` cannot do that -- its
    exit always joins -- which is why the one call site that needed to stop
    early had to reach in and shut the executor down by hand.
    """
    items = list(items)
    if not items:
        return
    name = label if isinstance(label, str) else "fan"
    ex = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(items))),
                            thread_name_prefix=name)
    try:
        futs = {ex.submit(fn, item): item for item in items}
        for fut in as_completed(futs):
            item = futs[fut]
            try:
                result = fut.result()
            except Exception as e:          # noqa: BLE001 - per item
                if on_error:
                    on_error(item, e)
                else:
                    what = label(item) if callable(label) else label
                    print(f"    [!] {what} error: {e}")
                continue
            yield (item, result) if with_item else result
    finally:
        # Reached on normal completion (nothing left running, so this is a
        # no-op) and on a break/throw/close (where it is the point).
        ex.shutdown(wait=False, cancel_futures=True)


def fetch_all(sources, max_workers=DEFAULT_WORKERS, on_done=None):
    """Run every (name, platform, thunk) source concurrently.

    Returns a list aligned with `sources`: each element is
    (jobs, error) where exactly one of the two is meaningful —
    `error` is None on success, and `jobs` is [] on failure.

    `on_done(name, platform, jobs, error)` fires on the caller's thread
    as each source completes (completion order), for progress output.
    """
    results = [([], None)] * len(sources)
    with ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix="fetch") as pool:
        futures = {pool.submit(spec[2]): i for i, spec in enumerate(sources)}
        for fut in as_completed(futures):
            i = futures[fut]
            name, platform, _ = sources[i]
            try:
                jobs, err = (fut.result() or []), None
            except Exception as e:
                jobs, err = [], e
            results[i] = (jobs, err)
            if on_done:
                on_done(name, platform, jobs, err)
    return results
