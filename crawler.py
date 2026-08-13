#!/usr/bin/env python3
"""DEPRECATED shim — the CLI moved to run_scraper.py.

Kept so existing scheduled tasks / scripts invoking crawler.py keep working;
all arguments are forwarded. Note the no-args behavior changed with the
retirement of the classic keyword crawl: no args now refreshes EVERY
configured track (profile.toml [tracks.*]) via the unified runner.

Legacy track flags are translated: `--track local-tech` / `--track
remote-neural` resolve against each track's jobs.track value, and the old
remote-neural passthrough flags (--commit/--fit) map to the new ones.
"""

import sys


def main():
    print("  [crawler.py is deprecated - forwarding to run_scraper.py]",
          file=sys.stderr)
    argv = sys.argv[1:]
    # Legacy remote-neural flags: --commit was opt-in persistence (default
    # preview) and --fit opt-in scoring; run_scraper inverts both defaults.
    if "--track" in argv:
        i = argv.index("--track")
        if i + 1 < len(argv) and argv[i + 1] == "remote-neural":
            if "--commit" in argv:
                argv.remove("--commit")
            else:
                argv.append("--preview")
            if "--fit" in argv:
                argv.remove("--fit")
            else:
                argv.append("--no-fit")
    if "--local-clinical" in argv or "--local-tech" in argv:
        argv = [a for a in argv
                if a not in ("--local-clinical", "--local-tech")]
        argv += ["--track", "local-tech"]
    from run_scraper import main as run
    run(argv)


if __name__ == "__main__":
    main()
