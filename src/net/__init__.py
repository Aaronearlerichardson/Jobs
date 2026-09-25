"""The network layer: how bytes are fetched, and how politely.

    http.py      the one session on the network loop, run_sync, headers,
                 timeouts, the per-host limiter, fetch accounting
    robots.py    robots.txt parsing and the per-host crawl-delay
    parallel.py  the thread pools, their watchdogs, and a single-flight memo
    ddg.py       DuckDuckGo search
    util.py      small shared helpers (worker counts, id and date norms)
"""
