"""Applicant-tracking systems: how a board is recognised and read.

    signatures.py  recognise a hosted board from a URL or page body and
                   read its coordinates off the match (pure text)
    coords.py      those coordinates as the store's company columns
    registry.py    which ATS families the crawl fetches, and how politely
    fetchers/      one module per ATS, all returning the same job dicts

Nothing here knows about tracks, ranking or the digest: an ATS module
turns board coordinates into job dicts and stops. It reads config, match
and net, and nothing above them.

`dork.py` used to live here, on the strength of its subject being ATS
URLs. What it actually does is source companies -- it mines search
results, scores what it finds and upserts it -- so it imported
src/discovery and src/store, and was the only reason this package did
either. That made src/ats and src/discovery mutually dependent, which
discovery worked around with a deferred import. It is src/discovery/
dork.py now and the cycle is gone.
"""
