"""Applicant-tracking systems: how a board is recognised and read.

    signatures.py  recognise a hosted board from a URL or page body and
                   read its coordinates off the match (pure text)
    coords.py      those coordinates as the store's company columns
    registry.py    which ATS families the crawl fetches, and how politely
    fetchers/      one module per ATS, all returning the same job dicts
    dork.py        mine search-engine-indexed board URLs for new boards

Nothing here knows about tracks, ranking or the digest: an ATS module
turns board coordinates into job dicts and stops.
"""
