"""Discovery: find employers worth crawling, and find their boards.

Two layers, and the directories say so:

    resolve/           given a NAME, find its board -- candidate URLs,
                       the identity guard, the careers-page sniffer, the
                       slug probes, the web-search fallback. Answers a
                       question and returns; touches no store.
    everything here    SOURCING: where names come from (seeds, bciwiki,
                       name_sources, paste_ingest, dork), what to do with
                       what resolve finds (local_sourcing, pipeline), and
                       how it reaches the roster (apply).

The split was already true of the imports: resolve/ uses only itself,
and the sourcing modules use resolve/. See src/discovery/resolve for why
that ordering is load-bearing.
"""

from .apply import apply_to_store
from .bciwiki import bciwiki_seed_candidates
from .pipeline import (
    discover,
    discover_companies,
    print_summary,
    write_discovery_report,
)

__all__ = [
    "apply_to_store",
    "bciwiki_seed_candidates",
    "discover",
    "discover_companies",
    "print_summary",
    "write_discovery_report",
]
