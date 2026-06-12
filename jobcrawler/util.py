"""Small shared helpers."""

import hashlib


def stable_id(*parts) -> str:
    """Deterministic short hash for building job IDs.

    Python's built-in hash() is salted per process (PYTHONHASHSEED), so
    IDs built from it change every run and the seen-jobs dedupe never
    matches — every RSS/scrape job re-surfaces as "new" forever. This
    sha1-based ID is stable across runs and machines.
    """
    key = "||".join(str(p) for p in parts)
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
