"""How a front end runs an operation by name.

    registry.py     name -> {label, engine, target, params}: the ONE table
                    the web UI's buttons, run_scraper.py's flags and
                    discover.py's flags all dispatch through
    background.py   the web UI's one-op-at-a-time runner and console tee

The top of the src/ import graph: the targets live in src/crawl, src/ops
and src/discovery, and only src/web and the root entry scripts import this
package, so the harvester never loads it.
"""
