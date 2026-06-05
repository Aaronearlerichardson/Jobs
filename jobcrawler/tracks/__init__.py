"""Crawl "tracks" — self-contained crawl configurations.

Each track bundles its own keyword focus, source list, eligibility filter,
and digest tag, layered on top of the shared fetchers / db / config. Tracks
live in their own modules so independent tracks (e.g. remote-neural vs
local-clinical-ml) can be developed and merged without stepping on each
other or on the default ``crawler.py`` path.
"""
