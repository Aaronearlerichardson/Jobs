#!/usr/bin/env python3
"""Job-crawler web UI — launch entry.

    python webapp.py            ->  http://127.0.0.1:5533

The application lives in src/web/ (routes.py, server.py, templates/,
static/) and its operations in src/ops/; this file only starts it.
"""

import sys

from src.web.server import main

if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code:
            print(e.code if isinstance(e.code, str) else f"exit {e.code}")
    except Exception as e:
        print(f"\n  [!] failed to start: {type(e).__name__}: {e}")
        # Double-clicked console windows vanish on exit — hold them open so
        # the error is actually readable.
        try:
            if sys.stdin and sys.stdin.isatty():
                input("  Press Enter to close...")
        except EOFError:
            pass
