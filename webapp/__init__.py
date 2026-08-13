"""Flask web UI package for the job crawler.

Layout: routes.py (the /api/* endpoints + the SPA), ops.py (the background
operation registry + runner), server.py (port logic, graceful self-restart,
main()). The SPA lives in templates/index.html + static/css|js. Root-level
webapp.py is the thin launch entry (`python webapp.py`).

Single-user by design — one operation at a time, same SQLite stores as the
CLI (run_scraper.py).
"""

import os
import sys
import uuid

from flask import Flask

try:  # Windows consoles default to cp1252; job text carries em-dashes etc.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

app = Flask(__name__, static_folder="static", static_url_path="/static")

# Changes on every process start; the restart overlay polls /api/stats until
# this differs from the value it remembered, i.e. the successor is up.
BOOT_ID = uuid.uuid4().hex

# Mutable server state shared across the package: routes flip `restarting`
# when a config save schedules the self-restart; server.main() records the
# actually-bound port so the restart successor inherits it.
STATE = {"bound_port": int(os.environ.get("WEBUI_PORT", "5533")),
         "restarting": False}

from . import routes  # noqa: E402,F401  — registers the @app routes

# Re-exports: the public surface tests and tooling poke at.
from .ops import OPS  # noqa: E402,F401
from .routes import _geo_tag  # noqa: E402,F401
from .server import main  # noqa: E402,F401
