"""Where the code and the install live: SCRIPT_DIR and APP_HOME.

Imports nothing of the package, so config.secrets (the `.env` file) and
config.paths (the data directory) can both start from the same root.

  SCRIPT_DIR - where the CODE lives (the checkout root; the exe's dir when
               compiled). Bundled read-only assets live here.
  APP_HOME   - the checkout / install root. From source: SCRIPT_DIR.
               Compiled: probed (see `app_home`), because the dist folder
               may sit INSIDE the project checkout, whose root holds the
               real data.

A compiled module's `__file__` points into the onefile unpack directory, not
the install, so nothing there may be located from `__file__`: that is why the
`.env` lookup (config.secrets) starts from APP_HOME.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Current DB filename, then the pre-rename one: probed when deciding whether
# a directory is an existing install.
DB_NAMES = ("jobs.db", "local_tech.db")


def app_home(exe_dir: Path) -> Path:
    """The install root of a compiled build running from `exe_dir`: the first
    of the exe's own folder (the copied-to-another-machine layout) and the
    folder above it (dist still inside the checkout) that holds a profile, a
    store or a data folder, else the exe's folder.

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as t:
    ...     root = Path(t); (root / "dist").mkdir(); (root / "data").mkdir()
    ...     app_home(root / "dist") == root, app_home(root) == root
    (True, True)
    >>> with tempfile.TemporaryDirectory() as t:
    ...     app_home(Path(t)) == Path(t)
    True
    """
    return next((d for d in (exe_dir, exe_dir.parent)
                 if (d / "profile.toml").exists()
                 or any((d / n).exists() for n in DB_NAMES)
                 or (d / "data").is_dir()),
                exe_dir)


if "__compiled__" in globals():
    SCRIPT_DIR = Path(sys.argv[0]).resolve().parent
    APP_HOME = app_home(SCRIPT_DIR)
else:
    # This file is <root>/src/config/home.py, so the checkout root is two
    # levels above the package. Counted from the path itself rather than
    # hardcoded: when the package tree moved under src/ a hardcoded
    # `.parent.parent` silently repointed DATA_DIR from the user's real
    # store to an empty per-user one -- the app came up working, on
    # nothing. tests/test_config_env.py pins it.
    SCRIPT_DIR = Path(__file__).resolve().parents[2]
    APP_HOME = SCRIPT_DIR
