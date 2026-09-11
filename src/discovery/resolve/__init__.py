"""Resolution: given a company NAME, find its job board.

    fetchpool.py         candidate URLs for a name, fetched in parallel
                         through a per-run memo and a dead-host cache
    identity.py          is this page really that company's? -- the risky
                         domain-token guard, and the one walk over a
                         company's candidate pages that applies it
    sniffer.py           read the ATS off a careers page (+ a headless
                         browser fallback for JS-rendered ones)
    probes.py            confirm a handle names a live board, and count it
    websearch_board.py   the same question asked of a search engine

The half of discovery that answers one question and returns. Nothing here
knows what a track is, touches the store, or decides whether a company is
worth keeping -- the modules one level up (local_sourcing, pipeline,
paste_ingest, dork) source the NAMES, ask these for coordinates, score
what comes back and write it.

That split was already true of the imports before it was true of the
directories: these five use only each other, and everything above uses
them. It is a directory now so the next reader can see it without
building the graph.
"""

from .fetchpool import ROOT_PATTERNS, candidate_urls
from .identity import candidate_pages, corroborated
from .probes import PROBES, probe_workday
from .sniffer import diagnose_no_board, sniff_ats, sniff_careers_ats

__all__ = [
    "ROOT_PATTERNS",
    "candidate_urls",
    "candidate_pages",
    "corroborated",
    "PROBES",
    "probe_workday",
    "diagnose_no_board",
    "sniff_ats",
    "sniff_careers_ats",
]
