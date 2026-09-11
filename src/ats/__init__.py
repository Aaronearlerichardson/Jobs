"""Applicant-tracking systems: how a board is recognised and read.

    signatures.py  recognise a hosted board from a URL or page body and
                   read its coordinates off the match (pure text)
    coords.py      those coordinates as the store's company columns
    registry.py    which ATS families the crawl fetches, and how politely
    fetchers/      one module per ATS, all returning the same job dicts

Nothing here knows about tracks, ranking or the digest: an ATS module
turns board coordinates into job dicts and stops. It reads config, match
and net and nothing above them -- no store, no discovery, not even
through a deferred import.

Two things used to break that, both filed here because their SUBJECT was
ATS boards while their WORK was sourcing companies:

    dork.py                       mined search results for board URLs,
                                  scored them and upserted them. Made
                                  src/ats and src/discovery mutually
                                  dependent. Now src/discovery/dork.py.
    getro.attribute_employers     matched an aggregator board's employers
                                  to the roster and queued the unknown
                                  ones for review -- a store WRITE inside
                                  a fetcher. Now in
                                  src/discovery/apply.py, beside the
                                  other "turn a discovery into a roster
                                  row" code.

Subject and layer are different axes. A module belongs where its
dependencies point, not where its topic sounds like it fits.
"""
