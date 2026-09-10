"""The free gates: everything that judges a posting without spending money.

    gates.py          technical-title and per-track exclude tables
    filters.py        keyword relevance over title + body
    locality.py       geography: is this posting in your area, or remote
    remote_filter.py  the remote-work phrases locality leans on
    names.py          company-name hygiene (junk names, slug guesses)

Pure text in, verdict out -- no network, no API, no store. The crawl, the
harvest triage pass and the ranking all read the same rules from here.
"""
