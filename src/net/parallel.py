"""Parallel source fetching.

Fetchers are network-bound and independent per source, so a small thread
pool takes a ~30-source crawl from minutes (serial + sleeps) to roughly
the slowest single source. Per-source rate limiting stays inside each
fetcher (their internal sleeps still apply); the pool only removes the
dead time *between* sources.

Results are returned in input order so callers can process priority
sources first and keep dedupe deterministic regardless of completion
"""

import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed
from concurrent.futures import wait as fut_wait
from contextlib import contextmanager

from src import config
from . import http
from .util import worker_count

# Network-I/O-bound, so this is a concurrency knob, not a CPU one: defaults
# to n_cpus-1, raise CRAWLER_WORKERS to push more concurrent source fetches.
DEFAULT_WORKERS = worker_count("crawler_workers")

# Stall watchdog: abandon a pool's remaining work if NOTHING completes for
# this long. Generous on purpose - a normal resolution chains a handful of
# bounded fetches; only a genuinely wedged one exceeds this.
RESOLVE_STALL_S = 300.0


@contextmanager
def pool(max_workers, name=""):
    """A thread pool whose queued work is cancelled however the block ends.

    A return, an exception and Ctrl+C alike: an item that has not started
    never does, and nothing waits for one that has (tests/test_harvest.py::
    test_ctrl_c_mid_wait_never_starts_the_queued_work). Every pool is built
    here (tests/test_invariants.py::POOL_OWNERS).

    Notes:
        A running item cannot be interrupted: it runs to its own end on its
        thread, each request in it bounded by its timeout, and the
        interpreter's exit waits for it (harvest.py leaves through os._exit
        instead). `with ThreadPoolExecutor()` joins on the way out, and the
        stdlib worker runs every QUEUED item before it looks at the
        shutdown flag; harvest.run and drain_or_abandon cancelled their
        queue only after a loop that ended normally, so Ctrl+C left the
        whole queue running (2026-09-17 reresolve log).
    """
    ex = ThreadPoolExecutor(max_workers=max(1, max_workers),
                            thread_name_prefix=name)
    try:
        yield ex
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _settled(futs, label, abandoned, stall_s=None, budget_s=None):
    """Each future of `futs` ({future: item}) as it completes, until none
    completes for `stall_s` or, given `budget_s`, that long has passed.
    Every one left then is reported as abandoned under label(item) and
    handed to abandoned(item); the caller's pool cancels the queued ones."""
    if stall_s is None and budget_s is None:
        # One waiter for the whole pass: the loop below installs one per
        # pending future per wait, O(n^2) over a large unbounded fan_out.
        yield from as_completed(futs)
        return
    end = None if budget_s is None else time.monotonic() + budget_s
    pending = set(futs)
    while pending:
        wait_s = stall_s if end is None else max(0.0, end - time.monotonic())
        done, pending = fut_wait(pending, timeout=wait_s,
                                 return_when=FIRST_COMPLETED)
        yield from done
        if not done and (end is None or time.monotonic() >= end):
            why = (f"no progress in {stall_s:g}s" if end is None
                   else f"past its {budget_s:g}s budget")
            for fut in pending:
                sys.stdout.write(f"    [!] {label(futs[fut])}: {why} - abandoned\n")
                abandoned(futs[fut])
            return


def drain(items, fn, consume, stalled, label=str,
          max_workers=DEFAULT_WORKERS):
    """`fan_out`'s sibling for work that can WEDGE rather than fail.

    Runs `fn` over `items` in a pool: `consume(future, label)` per
    completion, on the caller's thread. When nothing completes for
    RESOLVE_STALL_S, each item left is reported, passed to
    `stalled(label)` and abandoned (see `pool`).

    The choice between this and `fan_out` is whether the work can hang: a
    bounded API call cannot, a company resolution chaining page fetches
    can.

    Notes:
        Only a stall watchdog frees the caller: fetch_company_nc on a
        sprawling "custom board" is bounded per request, not in total,
        and 2026-08-28 resolved 59 of 60 names in 8 minutes, then held the
        web UI's one-op slot >1h on the last. Enforced by
        tests/test_parsers.py::TestResolutionStallWatchdog.
    """
    items = list(items)
    if not items:
        return
    with pool(min(max_workers, len(items))) as ex:
        futs = {ex.submit(fn, x): label(x) for x in items}
        for fut in _settled(futs, str, stalled, stall_s=RESOLVE_STALL_S):
            consume(fut, futs[fut])


def fan_out(items, fn, label="task", max_workers=DEFAULT_WORKERS,
            with_item=False, on_error=None, budget_s=None, on_abandon=None):
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

    `budget_s` bounds the whole pass. Past it, each item not yet done is
    reported abandoned, passed to `on_abandon(item)` and never yielded, so
    a consumer that writes only what it is handed records nothing for it:

    >>> import threading
    >>> hung = threading.Event()
    >>> list(fan_out([1, 0], lambda n: n or hung.wait(), str, budget_s=0.2))
        [!] 0: past its 0.2s budget - abandoned
    [1]
    >>> hung.set()

    A consumer may `break` out; the pool's rule (see `pool`) applies.
    """
    items = list(items)
    if not items:
        return
    what = label if callable(label) else (lambda _item: label)
    with pool(min(max_workers, len(items)),
              label if isinstance(label, str) else "fan") as ex:
        futs = {ex.submit(fn, item): item for item in items}
        for fut in _settled(futs, what, on_abandon or (lambda _item: None),
                            budget_s=budget_s):
            item = futs[fut]
            try:
                result = fut.result()
            except Exception as e:          # noqa: BLE001 - per item
                if on_error:
                    on_error(item, e)
                else:
                    print(f"    [!] {what(item)} error: {e}")
                continue
            yield (item, result) if with_item else result


def _accounted(thunk):
    """(jobs, snapshot) for one source, with this pool thread's fetch
    accounting reset first: a reused thread must not hand one source's
    failures or cap to the next."""
    http.reset_fetch_failures()
    jobs = thunk() or []
    return jobs, http.snapshot_info()


def fetch_all(sources, max_workers=DEFAULT_WORKERS, on_done=None,
              budget_s=None):
    """Run every (name, platform, thunk) source concurrently.

    Returns a list aligned with `sources`: each element is
    (jobs, error, snapshot). `error` is None on success; on failure `jobs`
    is [] and `snapshot` None. `snapshot` is net.http.snapshot_info() for
    that source's own fetch, so a caller can tell a complete board from an
    incomplete or capped one. One pool thread serving two sources does not
    carry the first one's failure into the second:

    >>> from src.net.http import fetch_failed
    >>> srcs = [("bad", "x", lambda: fetch_failed("bad", "timeout", indent=0)),
    ...         ("ok", "x", lambda: [1])]
    >>> [(jobs, snap["incomplete"])
    ...  for jobs, _, snap in fetch_all(srcs, max_workers=1)]
    [!] bad: timeout
    [([], True), ([1], False)]

    A thunk that raises is the `error` arm:

    >>> def boom():
    ...     raise OSError("refused")
    >>> fetch_all([("dead", "x", boom)])
    [([], OSError('refused'), None)]

    So is a source still unfinished when `budget_s` (default
    config.FETCH_BUDGET_S) runs out: it is reported abandoned, and no
    caller reconciles or closes anything from it.

    >>> import threading
    >>> hung = threading.Event()
    >>> fetch_all([("slow", "x", hung.wait)], budget_s=0.2)
        [!] slow: past its 0.2s budget - abandoned
    [([], TimeoutError('past its 0.2s budget'), None)]
    >>> hung.set()

    `on_done(name, platform, jobs, error)` fires on the caller's thread
    as each source completes (completion order), for progress output.
    """
    budget_s = config.FETCH_BUDGET_S if budget_s is None else budget_s
    results = [([], None, None)] * len(sources)

    def _abandon(i):
        results[i] = ([], TimeoutError(f"past its {budget_s:g}s budget"), None)

    with pool(max_workers, "fetch") as ex:
        futures = {ex.submit(_accounted, spec[2]): i
                   for i, spec in enumerate(sources)}
        for fut in _settled(futures, lambda i: sources[i][0], _abandon,
                            budget_s=budget_s):
            i = futures[fut]
            name, platform, _ = sources[i]
            try:
                (jobs, snap), err = fut.result(), None
            except Exception as e:
                jobs, err, snap = [], e, None
            results[i] = (jobs, err, snap)
            if on_done:
                on_done(name, platform, jobs, err)
    return results


class SingleFlight:
    """A per-key memo that concurrent callers of one key fill ONCE.

    `do(key, make, ttl)` returns the value kept for `key`, else make()'s:
    a caller arriving while another runs make() for the same key waits
    for it and reads the value it kept. A value is kept for `ttl` seconds
    (for good when None), and only when `keep(value)` holds; with nothing
    kept, each caller runs make() itself, one at a time.

    >>> calls = []
    >>> memo = SingleFlight(keep=lambda v: v is not None)
    >>> [memo.do("k", lambda: calls.append(1) or "v") for _ in "ab"], len(calls)
    (['v', 'v'], 1)
    >>> [memo.do("n", lambda: calls.append(1)) for _ in "ab"], len(calls)
    ([None, None], 3)

    `hold(key)` is the lock a maker of `key` holds, for a value kept
    somewhere else (the board engine's settled handle parts).
    """

    def __init__(self, keep=None):
        self._keep = keep
        self._memo = {}                 # key -> (expires or None, value)
        self._locks = {}
        self._guard = threading.Lock()

    def clear(self):
        """Forget every kept value."""
        self._memo.clear()

    def hold(self, key):
        """The lock one maker of `key` holds at a time."""
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _kept(self, key):
        got = self._memo.get(key)
        return got if got and (got[0] is None or time.monotonic() < got[0]) else None

    def do(self, key, make, ttl=None):
        """The value kept for `key`, else make()'s (see the class)."""
        got = self._kept(key)
        if got is None:
            with self.hold(key):
                got = self._kept(key)
                if got is None:
                    value = make()
                    got = (None if ttl is None else time.monotonic() + ttl, value)
                    if self._keep is None or self._keep(value):
                        self._memo[key] = got
        return got[1]
