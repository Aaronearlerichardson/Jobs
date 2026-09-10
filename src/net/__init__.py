"""The network layer: how bytes are fetched, and how politely.

    http.py      the shared session, headers, timeouts, retry policy
    robots.py    robots.txt parsing and the per-host crawl-delay
    parallel.py  the bounded fetch pool and its stall watchdog
    ddg.py       DuckDuckGo search
    util.py      small shared helpers (worker counts, id and date norms)
"""
