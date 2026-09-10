"""Where the code, the install and YOUR data live.

A leaf: imports nothing from the repo, so anything (a bootstrap, a log
opener, a build script) can learn the data directory without pulling in
the profile, tags or the track tables.

Three roots:
  SCRIPT_DIR — where the CODE lives (the checkout root; the exe's dir
               when compiled). Bundled read-only assets live here.
  APP_HOME   — the checkout / install root. From source: SCRIPT_DIR.
               Compiled: probed (see below), because the dist folder may sit
               INSIDE the project checkout, whose root holds the real data.
  DATA_DIR   — where YOUR data lives (DBs, résumé, profile.toml,
               job_reports/, caches, captures, backups). Resolved by
               `_resolve_data_dir` below — by default a per-user directory
               on your machine, OUTSIDE the checkout, so cloning the repo
               gives you the stock experience and your own data survives
               `git pull`, a re-clone, or deleting the checkout.
"""

import os
import sys
from pathlib import Path

APP_NAME = "JobCrawler"

# Current DB filename, then the pre-rename one — probed when deciding whether
# a directory is an existing install.
_DB_NAMES = ("jobs.db", "local_tech.db")


def _env(name):
    """A non-blank env var, else "" (the same blank-is-unset rule as
    config.secrets.env, repeated here so this module stays a leaf)."""
    return (os.environ.get(name) or "").strip()


def _platform_data_dir():
    """The conventional per-user application-data directory for this OS.

    Windows: %LOCALAPPDATA%\\JobCrawler
    macOS:   ~/Library/Application Support/JobCrawler
    Linux:   $XDG_DATA_HOME/job-crawler (default ~/.local/share/job-crawler)
    """
    if sys.platform == "win32":
        base = _env("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = _env("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "job-crawler"


def _looks_like_install(d):
    """True if `d` already holds this app's data — a store, or a profile."""
    return ((d / "profile.toml").exists()
            or any((d / n).exists() for n in _DB_NAMES))


def _resolve_data_dir(app_home):
    """Where this machine's data lives, in precedence order:

    1. JOBS_DATA_DIR — an explicit override, always wins.
    2. <app_home>/data — an EXISTING in-checkout data dir. Never orphan an
       install that predates the per-user default, and an easy opt-in for
       anyone who deliberately wants portable/self-contained data: make the
       folder and it is used.
    3. <app_home> itself, if a DB sits there — the legacy flat layout.
    4. The per-user OS data directory. The default for a fresh clone.
    """
    override = _env("JOBS_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if (app_home / "data").is_dir():
        return app_home / "data"
    if any((app_home / n).exists() for n in _DB_NAMES):
        return app_home
    return _platform_data_dir()


if "__compiled__" in globals():
    _exe_dir = Path(sys.argv[0]).resolve().parent
    SCRIPT_DIR = _exe_dir
    # APP_HOME: first place that looks like an install — the exe's own folder
    # (copied-to-another-machine layout), else the folder ABOVE the dist dir
    # (dist still inside the checkout), else the exe's folder.
    APP_HOME = next((d for d in (_exe_dir, _exe_dir.parent)
                     if _looks_like_install(d) or (d / "data").is_dir()),
                    _exe_dir)
else:
    # This file is config/paths.py; the code root is one level up.
    SCRIPT_DIR = Path(__file__).resolve().parent.parent
    APP_HOME = SCRIPT_DIR

DATA_DIR = _resolve_data_dir(APP_HOME)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# The store: companies (cached mission scores, scope tags) + jobs (dedup
# state, resume-fit scores, track membership). ONE file for every track —
# jobs.track is a comma-separated SET, so a posting that belongs to two
# tracks is one row visible to both (see store.track_set). A track can
# still get its own file via [tracks.*].db.
STORE_DB_PATH = DATA_DIR / "jobs.db"

REPORT_DIR = DATA_DIR / "job_reports"

# One cap for JD text everywhere — fetchers, hydration, DB storage, and the
# scoring prompt (see core/fit.py clip_desc, which keeps head + tail so
# a requirements block at the END of a long posting survives). The old
# per-site caps (2000/2500/4000) silently fed the scorer only the opening
# company boilerplate of long JDs: a 20k-char senior-manager posting had its
# disqualifying "8+ years TPM/GCP" block at char 7000 and scored 0.69 on the
# first 2500 chars. ~12k chars ≈ ~3k tokens per scoring call — the honesty
# is worth the marginal cost.
MAX_DESC_CHARS = 12000
