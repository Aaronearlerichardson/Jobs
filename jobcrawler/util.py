"""Small shared helpers."""

import hashlib
import os


def worker_count(env_var, floor=4):
    """Default thread-pool size: n_cpus - 1, overridable via `env_var`.

    Discovery and crawl fetching are network-I/O-bound (profiling a 677-
    company discovery run showed ~95% of wall time in socket/SSL reads and
    the headless browser, with the CPU near 10%). So threads mostly sit
    blocked on the network, and n_cpus-1 is a floor, not a ceiling — set
    the env var higher (e.g. 32) to push more concurrent requests and
    saturate the link. Adding CPU cores does NOT raise throughput here.
    """
    v = os.environ.get(env_var, "").strip()
    if v.isdigit() and int(v) > 0:
        return int(v)
    return max((os.cpu_count() or 9) - 1, floor)


def stable_id(*parts) -> str:
    """Deterministic short hash for building job IDs.

    Python's built-in hash() is salted per process (PYTHONHASHSEED), so
    IDs built from it change every run and the seen-jobs dedupe never
    matches — every RSS/scrape job re-surfaces as "new" forever. This
    sha1-based ID is stable across runs and machines.
    """
    key = "||".join(str(p) for p in parts)
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
