"""Parallel source fetching.

Fetchers are network-bound and independent per source, so a small thread
pool takes a ~30-source crawl from minutes (serial + sleeps) to roughly
the slowest single source. Per-source rate limiting stays inside each
fetcher (their internal sleeps still apply); the pool only removes the
dead time *between* sources.

Results are returned in input order so callers can process priority
sources first and keep dedupe deterministic regardless of completion
order.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_WORKERS = 8


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
