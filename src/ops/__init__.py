"""Every operation a front end can run, declared once.

    registry.py     name -> {label, engine, target, params}: the ONE table
                    the web UI's buttons, run_scraper.py's flags and
                    discover.py's flags all dispatch through
    maintenance.py  the store-maintenance targets (sync, verify, rescore,
                    the backfills, re-resolution, manual adds)
    roster.py       the composite targets: open a track's store, call one
                    thing, report what it did
    background.py   the web UI's one-op-at-a-time runner and console tee

A front end hands `invoke()` an op name and a flat params dict; nothing
else in here is front-end specific.
"""

from .registry import REGISTRY, invoke, ui_ops                # noqa: F401
