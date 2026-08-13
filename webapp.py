#!/usr/bin/env python3
"""Job-crawler web UI — launch entry.

    python webapp.py            ->  http://127.0.0.1:5533

The application lives in the webapp/ package (routes.py, ops.py, server.py,
templates/, static/); this file only starts it. When both this module and
the package share the name, `import webapp` resolves to the package — this
script runs as __main__, so there is no collision.
"""

import sys

from webapp.server import main

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
