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
