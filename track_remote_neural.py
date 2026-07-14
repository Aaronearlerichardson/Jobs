#!/usr/bin/env python3
"""Back-compat shim. The REMOTE-NEURAL runner now lives in
jobcrawler/tracks/remote_neural_run.py; prefer the single entry point:

    python crawler.py --track remote-neural [--commit] [--send] [--fit] ...
"""

from jobcrawler.tracks.remote_neural_run import main

if __name__ == "__main__":
    main()
