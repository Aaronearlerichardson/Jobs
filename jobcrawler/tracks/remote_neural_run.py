"""REMOTE-NEURAL track runner — back-compat CLI shim.

The crawl itself lives in jobcrawler/tracks/runner.py: ONE pipeline for
every track, parameterized by profile.toml [tracks.*] (keyword_mode,
sources, gates, cost_guard, email — see config._ENGINE_CRAWL_DEFAULTS for
the neural engine's defaults: location-agnostic sweep of the priority
companies + tag-scoped store roster + aggregator feeds + web search, with a
core-anchor gate and remote_eligible stamped rather than gated).

This module keeps the historical CLI (crawler.py --remote-neural /
track_remote_neural.py) working: same flags, same behavior, now delegating
to the unified runner for the default neural-engine track.
"""

import argparse
import sys

# Windows consoles default to cp1252; job blurbs carry em-dashes, curly
# quotes, and the odd emoji. Print defensively rather than crash mid-run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Back-compat: the neural engine's default scoring budget. The live value is
# per-track config now ([tracks.*].cost_guard); this constant remains for
# callers/docs that referenced it.
COST_GUARD_THRESHOLD = 300


def main(argv=None):
    ap = argparse.ArgumentParser(description="Remote-Neural job track")
    ap.add_argument("--commit", action="store_true",
                    help="Persist remote-eligible matches to the unified store")
    ap.add_argument("--send", action="store_true",
                    help="Email the tagged digest")
    ap.add_argument("--fit", action="store_true",
                    help="Resume-fit-score matches and rank the digest by fit")
    ap.add_argument("--no-websearch", action="store_true",
                    help="Skip the DuckDuckGo web-search sources")
    ap.add_argument("--samples", type=int, default=5,
                    help="Number of sample matches to print (default 5)")
    ap.add_argument("--confirm-cost", action="store_true",
                    help="With --fit: allow scoring more than the track's "
                         "cost_guard safety threshold via the Claude API")
    args = ap.parse_args(argv)

    # --send forces the email on even when the track config says email=false;
    # otherwise the config decides.
    from . import runner
    return runner.run_track(runner.track_for_engine("neural"),
                            fit=args.fit, commit=args.commit,
                            send=args.send or None,
                            websearch=not args.no_websearch,
                            confirm_cost=args.confirm_cost,
                            samples=args.samples)


if __name__ == "__main__":
    main()
