"""The network layer: how bytes are fetched, and how politely.

    http.py      the run's one session, headers, timeouts, the per-host
                 limiter, fetch accounting
    robots.py    robots.txt parsing and the per-host crawl-delay
    parallel.py  the task fan-outs, their watchdogs, and a single-flight memo
    ddg.py       DuckDuckGo search
    util.py      small shared helpers (worker counts, id and date norms)
"""
