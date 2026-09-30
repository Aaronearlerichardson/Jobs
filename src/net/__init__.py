"""The network layer: how bytes are fetched, and how politely.

    http.py      the run's one session, headers, timeouts, the per-host
                 limiter, fetch accounting
    robots.py    robots.txt parsing and the per-host crawl-delay
    parallel.py  the task fan-outs, their watchdogs, and a single-flight memo
    ddg.py       DuckDuckGo search
    util.py      small shared helpers (worker counts, id and date norms)
"""

from __future__ import annotations

# robots.py imports protego on the first polite request, so an environment
# that predates it would start, run, and turn every request into an error
# (2026-09-30: a discovery run probed 164 candidates and logged 45 "No module
# named 'protego'"). Failing here stops the program at start, with the fix.
try:
    import protego  # noqa: F401
except ImportError as e:
    raise ImportError("protego is not installed: run `pip install -r envs/requirements.txt` "
                      "(conda: `conda env update -f envs/environment.yml --prune`)") from e
