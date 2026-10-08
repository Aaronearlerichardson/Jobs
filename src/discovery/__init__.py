"""Discovery: find employers worth crawling, and find their boards.

Two layers, and the directories say so:

    resolve/           given a NAME, find its board -- candidate URLs,
                       the identity guard, the careers-page sniffer, the
                       slug probes, the web-search fallback. Answers a
                       question and returns; touches no store.
    everything here    SOURCING: where names come from (seeds,
                       name_sources, paste_ingest, registries, dork), what
                       to do with what resolve finds (local_sourcing,
                       pipeline), and how it reaches the roster (write,
                       apply).

The split was already true of the imports: resolve/ uses only itself,
and the sourcing modules use resolve/. See src/discovery/resolve for why
that ordering is load-bearing.
"""

from __future__ import annotations

from .pipeline import discover, print_summary, write_discovery_report

__all__ = [
    "discover",
    "print_summary",
    "write_discovery_report",
]
