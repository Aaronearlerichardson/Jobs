#!/usr/bin/env python3
r"""Build a standalone distribution of the web UI with Nuitka.

    python build_app.py                    # the web UI, for this platform
    python build_app.py --target harvest   # the background harvester
    python build_app.py --check            # print the command without running it

Output: single-file binary (`JobCrawlerUI.exe` or `job-crawler-ui`; the
harvester is `JobHarvester.exe` / `job-harvester`) —
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

Playwright (headless probes for JS-only boards) IS bundled, driver included —
that is most of the binary's size. Two separate pieces are needed to run a
headless browser, and only one of them can ship:

  * the DRIVER (~100 MB of node + JS under playwright/driver/) is package
    DATA, so Nuitka needs --include-package-data; without it the modules
    compile in but sync_playwright() cannot start at all.
  * the BROWSER itself is a separate ~150 MB per-platform download that
    lives outside the package, and is not bundled. The probes fall back to
    a Chrome or Edge already on the machine ([policy] browser_channels),
    which is what makes them work on a fresh install without asking anyone
    to run `playwright install`.

So: a machine with any Chrome or Edge gets working JS probes out of the box;
a machine with neither degrades gracefully, as before.

The first build downloads a C compiler if none is found and can take
10-30 minutes; rebuilds are much faster.

Run it with the conda environment ACTIVATED (build_exe.bat does this; from
a bare shell use `conda activate jobs` first, not just the env's python.exe
by path). Nuitka finds the DLLs the extension modules need by walking PATH,
and conda keeps them in <env>/Library/bin: built without that on PATH the
binary compiles cleanly and then dies on `import sqlite3` with
"LoadLibraryExW '_sqlite3.pyd' failed" because sqlite3.dll (and libssl,
libcrypto, ...) never made it in (JobHarvester.exe, 2026-09-10).
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

# --target NAME -> (entry script, Windows output name, other-platform name).
# The two binaries share the crawler packages but not the rest: see
# HARVEST_SKIP for what the harvester leaves out.
TARGETS = {
    "ui":      ("webapp.py",  "JobCrawlerUI.exe", "job-crawler-ui"),
    "harvest": ("harvest.py", "JobHarvester.exe", "job-harvester"),
}


def target():
    """The --target NAME from argv (default 'ui')."""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--target" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--target="):
            return a.split("=", 1)[1]
    return "ui"


def output_name(name=None):
    entry, win, other = TARGETS[name or target()]
    return win if sys.platform == "win32" else other


OUTPUT_NAME = output_name()

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
PACKAGES = ["core", "scrapers", "discovery", "webapp", "ddgs", "playwright",
            "fake_useragent"]

# Packages whose non-Python files must ship too. playwright/driver/ holds the
# node runtime and cli.js that sync_playwright() execs; playwright locates it
# as `Path(inspect.getfile(playwright)).parent / "driver"`, so it has to land
# beside the compiled module rather than anywhere else.
#
# fake_useragent is the same shape of problem as ddgs above, one level deeper:
# ddgs builds its request headers through it, and it reads its UA corpus from
# package data (data/browsers.jsonl) at import. Compiled without that file the
# module imports fine and then every search dies on
# `FakeUserAgentError: Failed to load or parse browsers.json` — so the dork
# sweep returned 0 results for every query in the packaged app while working
# normally from a source checkout.
DATA_PACKAGES = ["playwright", "fake_useragent"]

# The harvester never serves the UI, runs a dork sweep, or probes a JS-only
# board: it imports core + scrapers and nothing else at module level, and
# the only paths that reach playwright/ddgs are lazy, guarded imports in
# discovery that a whole-board pull does not take. Not following these at
# all is what makes the difference -- Nuitka would otherwise compile every
# module it can see through those lazy imports and ship the ~100 MB driver.
# Roughly a third of the C files and three quarters of the payload.
HARVEST_SKIP = ["webapp", "flask", "werkzeug", "jinja2", "playwright",
                "ddgs", "fake_useragent", "primp"]


def build_command(name=None):
    name = name or target()
    entry = TARGETS[name][0]
    cmd = [sys.executable, "-m", "nuitka", entry,
           "--onefile", f"--output-filename={output_name(name)}",
           "--assume-yes-for-downloads"]
    if name == "harvest" and sys.platform == "win32":
        # The harvester lives in the Startup folder and loops for the whole
        # session: launched from Explorer it must not park a console window
        # on the desktop, launched from a terminal it should still print.
        # "attach" does exactly that split; output always reaches the
        # session log either way.
        cmd.append("--windows-console-mode=attach")
    if name == "harvest":
        cmd += ["--include-package=core", "--include-package=scrapers"]
        cmd += [f"--nofollow-import-to={p}" for p in HARVEST_SKIP]
        cmd += [f"--include-data-files={src}={dst}" for src, dst in DATA_FILES
                if not src.startswith("webapp/")]
    else:
        cmd += [f"--include-package={p}" for p in PACKAGES]
        cmd += [f"--include-package-data={p}" for p in DATA_PACKAGES]
        cmd += [f"--include-data-files={src}={dst}" for src, dst in DATA_FILES]
        cmd += [f"--include-data-dir={src}={dst}" for src, dst in DATA_DIRS]
    # Any other --flag on our command line is Nuitka's (e.g.
    # --force-dll-dependency-cache-update after a build that ran without
    # the env on PATH cached "no DLL dependencies" for the extension
    # modules, and every later build inherited the gap).
    skip = {"--check"}
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a in skip or a == "--target" or a.startswith("--target=")                 or (i and argv[i - 1] == "--target"):
            continue
        if a.startswith("--"):
            cmd.append(a)
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
