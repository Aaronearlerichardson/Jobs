#!/usr/bin/env python3
r"""Build a standalone distribution of the web UI with Nuitka.

    python build_app.py            # build for the current platform
    python build_app.py --check    # print the command without running it

Output: single-file binary (`JobCrawlerUI.exe` or `job-crawler-ui`) —
self-contained (bundled CPython + Flask + the crawler packages + lxml),
copy it anywhere and run; no Python or pip needed on the target.

Data resolution at RUNTIME (config.py): JOBS_DATA_DIR if set; else a `data`
folder beside the binary, or beside its parent when the dist folder still
sits inside the checkout (so a build never spawns a second empty store next
to the project's real one); else the legacy flat layout; else the per-user
application-data directory (%LOCALAPPDATA%\JobCrawler, ~/Library/Application
Support/JobCrawler, $XDG_DATA_HOME/job-crawler) — the copied-to-a-new-machine
case, which starts a clean install rather than inheriting anything. The
running app prints the paths it chose; `run_scraper.py --where` prints them
without starting anything. Set ANTHROPIC_API_KEY in the environment for
scoring.

Playwright (optional headless probes for JS-only boards) is deliberately
excluded — it needs its own browser download and cannot ship in a binary;
those probes degrade gracefully without it.

The first build downloads a C compiler if none is found and can take
10-30 minutes; rebuilds are much faster.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
OUTPUT_NAME = "JobCrawlerUI.exe" if sys.platform == "win32" else "job-crawler-ui"

# Data files the app reads at runtime but Nuitka can't infer from imports.
DATA_FILES = [
    ("webapp/templates/index.html", "webapp/templates/index.html"),
    ("profile.example.toml", "profile.example.toml"),
]
DATA_DIRS = [("webapp/static", "webapp/static")]
# `ddgs` is listed explicitly even though it is only imported inside a function:
# it loads its search-engine backends by walking its own package directory at
# runtime, so following the import alone leaves the compiled build with the
# package but none of the engines, and every dork query dies on KeyError('text').
PACKAGES = ["core", "scrapers", "discovery", "webapp", "ddgs"]


def build_command():
    cmd = [sys.executable, "-m", "nuitka", "webapp.py",
           "--onefile", f"--output-filename={OUTPUT_NAME}",
           "--assume-yes-for-downloads"]
    cmd += [f"--include-package={p}" for p in PACKAGES]
    cmd += [f"--include-data-files={src}={dst}" for src, dst in DATA_FILES]
    cmd += [f"--include-data-dir={src}={dst}" for src, dst in DATA_DIRS]
    return cmd


def main():
    cmd = build_command()
    if "--check" in sys.argv:
        print(" ".join(cmd))
        return 0
    if subprocess.run([sys.executable, "-m", "pip", "show", "nuitka"],
                      capture_output=True).returncode:
        subprocess.run([sys.executable, "-m", "pip", "install", "nuitka"],
                       check=True)
    if subprocess.run([sys.executable, "-m", "pip", "show", "zstandard"],
                      capture_output=True).returncode:
        subprocess.run([sys.executable, "-m", "pip", "install", "zstandard"],
                       check=True)
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    # Nuitka --onefile produces the binary in the current directory or
    # output-dir, but the script previously expected it in webapp.dist/
    # (which --standalone creates). With --onefile, we'll just check the root.
    out = ROOT / OUTPUT_NAME
    if rc == 0 and out.exists():
        print(f"\nBuild OK: {out}")
    else:
        print(f"\nBuild FAILED - expected {out}")
        rc = rc or 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
