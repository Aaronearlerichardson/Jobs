"""Resolution: given a company NAME, find its job board.

    fetchpool.py         candidate URLs for a name, fetched in parallel
                         through a per-run memo and a dead-host cache
    identity.py          is this page really that company's? -- the risky
                         domain-token guard, the one walk over a company's
                         candidate pages that applies it, and the HQ
                         signal for "in your area at all?"
    sniffer.py           read the ATS off a careers page (+ a headless
                         browser fallback for JS-rendered ones)
    probes.py            confirm a handle names a live board and count it;
                         and, a layer up, probe a NAME by guessing slugs
                         and counting its local postings
    websearch_board.py   the same question asked of a search engine
    board.py             the top: name -> validated board, or the miss
                         reason that explains why not

The half of discovery that answers one question and returns. Nothing here
knows what a track is, touches the store, or decides whether a company is
worth keeping -- the modules one level up (local_sourcing, pipeline,
paste_ingest, dork) source the NAMES, ask these for coordinates, score
what comes back and write it.

That split was already true of the imports before it was true of the
directories: these modules use only each other, and everything above
uses them. It is a directory now so the next reader can see it without
building the graph.
"""

from .board import classify_miss, resolve_board_sniff_first, resolve_or_miss
from .fetchpool import ROOT_PATTERNS, candidate_urls
from .identity import candidate_pages, corroborated, nc_hq_signal
from .probes import PROBES, probe_company, probe_workday
from .sniffer import diagnose_no_board, sniff_ats, sniff_careers_ats

__all__ = [
    "ROOT_PATTERNS",
    "candidate_urls",
    "candidate_pages",
    "corroborated",
    "nc_hq_signal",
    "PROBES",
    "probe_company",
    "probe_workday",
    "diagnose_no_board",
    "sniff_ats",
    "sniff_careers_ats",
    "classify_miss",
    "resolve_board_sniff_first",
    "resolve_or_miss",
]
