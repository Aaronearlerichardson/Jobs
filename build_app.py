#!/usr/bin/env python3
r"""Build a standalone distribution of the web UI with Nuitka.

    python build_app.py                    # the web UI, for this platform
    python build_app.py --target harvest   # the background harvester
    python build_app.py --check            # print the command without running it

Output: single-file binary (`JobCrawlerUI.exe` or `job-crawler-ui`; the
harvester is `JobHarvester.exe` / `job-harvester`) —
self-contained (bundled CPython + Flask + the crawler packages + lxml),
copy it anywhere and run; no Python or pip needed on the target.

Data resolution at RUNTIME (src/config/paths.py): JOBS_DATA_DIR if set; else a `data`
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
    DATA; without it the modules compile in but sync_playwright() cannot
    start at all. Nuitka ships it for us: its bundled package config
    (nuitka/plugins/standard/standard.nuitka-package.config.yml, entry
    `module-name: 'playwright'`) already claims **/*.json, **/*.js,
    **/*.ts, **/*.html, **/*.css and **/*.svg under the package and
    registers driver/node* as an executable DLL. We therefore do NOT pass
    --include-package-data=playwright — doing so only duplicated what the
    plugin already had, and printed 124 "Duplicate data file
    'playwright\driver\...' ... already provided via 'package 'playwright'
    package data' -- ignored" warnings in build.log (2026-09-11).
  * the BROWSER itself is a separate ~150 MB per-platform download that
    lives outside the package, and is not bundled. The probes fall back to
    a Chrome or Edge already on the machine ([policy] browser_channels),
    which is what makes them work on a fresh install without asking anyone
    to run `playwright install`.

So: a machine with any Chrome or Edge gets working JS probes out of the box;
a machine with neither degrades gracefully, as before.

The first build downloads a C compiler if none is found and can take
10-30 minutes; rebuilds are much faster.

A onefile binary has to unpack itself before it can run, and both targets
are built to unpack ONCE per version, into
%LOCALAPPDATA%\JobCrawler\cache\<product>\<version>\ (Nuitka's
'{CACHE_DIR}/{COMPANY}/cache/{PRODUCT}/{VERSION}'), instead of into a fresh
temporary folder on every launch. That matters most for the UI, which
relaunches itself on every Settings save (src/web/server.py
schedule_restart()) and so re-extracted its whole ~189 MB payload each
time. The version comes from `git describe --tags --long`, so a build off a
different commit gets a different folder; a rebuild of the SAME commit
reuses the folder and the launcher refreshes its contents (measured
2026-09-11, see build_command()). Delete the folder to reclaim the space;
the next launch writes it again.

Run it with the conda environment ACTIVATED (build_exe.bat does this; from
a bare shell use `conda activate jobs` first, not just the env's python.exe
by path). Nuitka finds the DLLs the extension modules need by walking PATH,
and conda keeps them in <env>/Library/bin: built without that on PATH the
binary compiles cleanly and then dies on `import sqlite3` with
"LoadLibraryExW '_sqlite3.pyd' failed" because sqlite3.dll (and libssl,
libcrypto, ...) never made it in (JobHarvester.exe, 2026-09-10).
"""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

#: The operation table whose "module:function" target strings the UI's
#: --include-module list is derived from (see include_modules() below).
REGISTRY_PY = ROOT / "src" / "ops" / "registry.py"

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

# One sentence per target for the Windows "Details" tab. Nuitka makes
# --file-description mandatory on Windows and defaults it to the bare
# filename, which tells a user staring at Task Manager nothing.
DESCRIPTIONS = {
    "ui":      "Job crawler web UI",
    "harvest": "Job crawler background board harvester",
}

# No tag, no git, no repo: a shipped binary still has to carry SOME version,
# and 0.0.0.0 is the one that reads as "unknown" rather than as a release.
UNKNOWN_VERSION = "0.0.0.0"


def parse_describe(text):
    """A 4-number Windows version from `git describe --tags --long` output.

    The first three components come from the tag (padded with zeros when
    the tag carries fewer, as v0.1 does), and the fourth is the number of
    commits since it, so every build off the same checkout gets a distinct
    version and the onefile cache below (keyed on {VERSION}) does not hand
    an old unpack to a new binary.

    >>> parse_describe("v0.1.1-37-gabcdef0")
    '0.1.1.37'
    >>> parse_describe("v0.1-158-g5525f88")
    '0.1.0.158'
    >>> parse_describe("3.4.5.6-0-gdeadbee")
    '3.4.5.0'
    >>> parse_describe("")
    '0.0.0.0'
    >>> parse_describe("no-tag-here")
    '0.0.0.0'
    """
    parts = text.strip().rsplit("-", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return UNKNOWN_VERSION
    tag = parts[0].lstrip("vV").split(".")
    nums = [p for p in tag[:3] if p.isdigit()]
    if not nums:
        return UNKNOWN_VERSION
    return ".".join(nums + ["0"] * (3 - len(nums)) + [parts[1]])


def version():
    """parse_describe() of the working tree, or UNKNOWN_VERSION off-repo."""
    try:
        out = subprocess.run(["git", "describe", "--tags", "--long"],
                             cwd=ROOT, capture_output=True, text=True)
    except OSError:
        out = None
    v = parse_describe(out.stdout) if out and not out.returncode else \
        UNKNOWN_VERSION
    if v == UNKNOWN_VERSION:
        print("note: no git tag found; building as " + UNKNOWN_VERSION)
    return v

# Data files the app reads at runtime but Nuitka can't infer from imports.
DATA_FILES = [
    ("src/web/templates/index.html", "src/web/templates/index.html"),
    ("profile.example.toml", "profile.example.toml"),
]
DATA_DIRS = [("src/web/static", "src/web/static")]

#: Our own entries in Nuitka's package-configuration format, for packages
#: whose hidden dependencies its bundled config does not already describe.
#: Passed to both targets; see the file's own comments for what is in it.
PACKAGE_CONFIG = "jobs.nuitka-package.config.yml"

# Packages that must be compiled in whole, because an import statement alone
# does not reach all of what they need at runtime.
#
# `ddgs` was listed here and is gone. It is the same problem as before -- it
# loads its search-engine backends by walking its own package directory at
# runtime, so following the import alone leaves the compiled build with the
# package but none of the engines, and every dork query dies on
# KeyError('text') -- but the fix is now one entry in PACKAGE_CONFIG that
# makes ddgs.engines depend on iterate_modules("ddgs.engines"). That follows
# exactly the engine modules the installed ddgs release ships, instead of
# also compiling in ddgs.cli and ddgs.api_server, which nothing here calls
# (2026-09-11).
PACKAGES = ["playwright", "fake_useragent"]

# Modules that must ship even though NO import statement reaches them, so
# Nuitka cannot see them and neither can the import graph
# `python tools/entrydeps.py` walks.
#
# src/ops/registry.py declares every operation a front end can start, and
# names its target as a "src.ops.roster:prune" STRING resolved with importlib
# at call time. That indirection is worth keeping -- making the 21 targets
# real imports costs 700 ms and 363 extra modules (bs4, lxml, requests) on
# every CLI invocation, measured -- but it is invisible to every static tool,
# Nuitka's import graph included.
#
# Both targets used to say --include-package=src, which covered this by
# shipping all 95 src modules. It also dragged in src/crawl/page_capture.py
# (473 lines only capture.py reaches, and capture.py is not compiled) and put
# --include-package=src in direct contradiction with
# --nofollow-import-to=src.web below. cc86d84 dropped it for a hand-written
# list of the modules believed to be unreachable otherwise, on the count that
# 93 of 95 were reachable by real imports.
#
# That count was right about imports and wrong about the binary, and the way
# it was wrong is why this list is no longer written by hand: on 2026-09-11
# JobCrawlerUI.exe (built from 5525f88) died on the first operation started
# from the web UI with "[!] operation failed: ModuleNotFoundError: No module
# named 'src.crawl'". `python tools/entrydeps.py webapp.py --modules` names
# ZERO src.crawl modules and, out of src/ops, only src.ops, src.ops.background
# and src.ops.registry -- so the compiled UI contained no crawler at all, and
# running the crawl is the UI's whole job. src.ops.maintenance (eight of the
# targets) was in the same position, shipping only if Nuitka happened to
# follow src.ops.roster's function-level imports.
#
# A hand-written list goes stale the first time someone adds an operation, and
# nothing fails until a button is pressed in a compiled build -- which is not
# something the test suite or CI can press. Deriving the list from the
# registry's own source is the only form of it that cannot drift: add a
# target, and the next build ships its module. We read that source with `ast`
# rather than importing it, because importing src.ops.registry imports
# src.config, which loads the profile and touches the user's data directory;
# a build script (and a doctest) must do neither.


def registry_targets(source):
    """The module half of every "module:attr" operation target in `source`.

    Sorted and de-duplicated, so two builds of the same tree get the same
    flags in the same order.

    >>> registry_targets('R = {"a": {"target": "src.ops.roster:prune"},'
    ...                  '     "b": {"target": "src.crawl.runner:run_track"},'
    ...                  '     "c": {"target": "src.ops.roster:dedup"}}')
    ['src.crawl.runner', 'src.ops.roster']
    >>> registry_targets('R = {"a": {"label": "Crawl", "engine": None}}')
    []
    """
    mods = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "target"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str) and ":" in value.value):
                mods.add(value.value.partition(":")[0])
    return sorted(mods)


def include_modules(path=REGISTRY_PY):
    """registry_targets() of the registry source on disk."""
    return registry_targets(Path(path).read_text(encoding="utf-8"))

# Packages whose non-Python files must ship too, and that Nuitka does not
# already know about itself.
#
# "playwright" was listed here and is gone. Its driver (the node runtime and
# cli.js that sync_playwright() execs) still ships: Nuitka 4.2.1's own
# package config -- nuitka/plugins/standard/standard.nuitka-package.config.yml,
# entry `module-name: 'playwright'` -- claims **/*.json, **/*.js, **/*.ts,
# **/*.html, **/*.css and **/*.svg as package data and registers driver/node*
# as an executable DLL. Our flag added nothing on top of that and cost 124
# "Nuitka-Inclusion:WARNING: Duplicate data file 'playwright\driver\...'
# ... already provided via 'package 'playwright' package data' -- ignored"
# lines in build.log (2026-09-11). Because the driver now depends on that
# bundled entry existing, envs/requirements-dev.txt pins nuitka>=4.2,<5.
#
# Nuitka has no such entry for ddgs, fake_useragent or primp, so those are
# still ours to declare -- ddgs through PACKAGE_CONFIG above, fake_useragent
# through the flag below.
#
# fake_useragent is the same shape of problem as ddgs above, one level deeper:
# ddgs builds its request headers through it, and it reads its UA corpus from
# package data (data/browsers.jsonl) at import. Compiled without that file the
# module imports fine and then every search dies on
# `FakeUserAgentError: Failed to load or parse browsers.json` — so the dork
# sweep returned 0 results for every query in the packaged app while working
# normally from a source checkout.
DATA_PACKAGES = ["fake_useragent"]

# The harvester never serves the UI, runs a dork sweep, or probes a JS-only
# board: it imports src.store, src.crawl and src.ats and nothing else at
# module level, and the only paths that reach playwright/ddgs are lazy,
# guarded imports in src/discovery that a whole-board pull does not take. Not following these at
# all is what makes the difference -- Nuitka would otherwise compile every
# module it can see through those lazy imports and ship the ~100 MB driver.
# Roughly a third of the C files and three quarters of the payload.
#
# Third-party only. "src.web" was listed here too and is gone: harvest.py
# cannot reach it by any import, deferred ones included
# (`python tools/entrydeps.py harvest.py --check` says so), and the entry
# only had to be written down at all because --include-package=src was
# forcing src.web in for the nofollow to fight back out.
# The two halves of that list are not the same claim, so they no longer get
# the same flag.
#
# HARVEST_FORBID is what the harvester CANNOT reach by any import, deferred
# ones included: `python tools/entrydeps.py harvest.py --check` says so, and
# nothing in a whole-board pull has any business serving HTTP. A nofollow
# here is silent -- if someone later imports flask from src/crawl the build
# still succeeds and the harvester grows a web stack nobody asked for. With
# --noinclude-custom-mode=flask:error the build FAILS instead and says which
# import did it. The choices Nuitka 4.2.1 accepts for this flag are "error",
# "warning" and "nofollow" (`--help`, 2026-09-11), and "error" really does
# abort: a one-line `import flask` script built with
# --standalone --noinclude-custom-mode=flask:error stops at "FATAL: Error,
# forbidden import of 'flask' (intending to avoid 'flask') in module
# '__main__'" (measured 2026-09-11). Note it only bites in standalone/onefile
# mode -- the same probe in Nuitka's default accelerated mode compiled
# happily, because nothing is being "included" there. Both our targets are
# --onefile, so it is live for both.
HARVEST_FORBID = ["flask", "werkzeug", "jinja2"]

# HARVEST_LAZY is the opposite case: src/discovery really does import these,
# behind guards a whole-board pull never takes. The import exists, so
# "error" would fail every build; we just decline to follow it.
HARVEST_LAZY = ["playwright", "ddgs", "fake_useragent", "primp"]

# The union, for tools/entrydeps.py --check, which reports which skips are
# doing real work.
HARVEST_SKIP = HARVEST_FORBID + HARVEST_LAZY


def build_command(name=None):
    name = name or target()
    entry = TARGETS[name][0]
    cmd = [sys.executable, "-m", "nuitka", entry,
           "--onefile", f"--output-filename={output_name(name)}",
           "--assume-yes-for-downloads"]
    # Both targets are plain console apps (Nuitka's default, "force"), so
    # each gets a window of its own however it was started.
    #
    # The harvester was built with --windows-console-mode=attach for a
    # while: launched from Explorer it showed nothing, launched from a
    # terminal it printed. Tidier on the desktop, and wrong in practice --
    # a process that loops for the whole session with no window is a
    # process you have to go into Task Manager to stop, and you cannot see
    # what it is doing without opening a log. The window IS the off switch
    # (close it, or Ctrl+C, which harvest.py already handles cleanly), and
    # it is the same handle the UI gives you.
    #
    # Version information, so the binary is identifiable from Explorer's
    # Details tab and from Task Manager, and -- the reason it is not just
    # cosmetic -- so {COMPANY}/{PRODUCT}/{VERSION} below resolve to a path
    # that changes whenever the payload does.
    v = version()
    cmd += ["--company-name=JobCrawler",
            f"--product-name={TARGETS[name][1].removesuffix('.exe')}",
            f"--file-version={v}", f"--product-version={v}",
            f"--file-description={DESCRIPTIONS[name]}"]
    # Unpack once per version instead of once per launch. The default spec
    # is '{TEMP}/onefile_{PID}_{TIME_US}_{RANDOM}', i.e. a fresh extraction
    # every single run, and src/web/server.py schedule_restart() relaunches
    # sys.argv[0] on every Settings save -- so saving a setting in the UI
    # re-extracted the whole ~189 MB payload. A static path under the
    # per-user cache directory is reused, and {VERSION} keeps two builds
    # off different tags from ever sharing one folder.
    #
    # The obvious worry with a static path is a STALE unpack: two builds of
    # the same commit carry the same {VERSION}, so they land on the same
    # folder. Measured on 2026-09-11, they do not go stale. Built the
    # harvester, ran it, hashed the unpacked
    # %LOCALAPPDATA%\JobCrawler\JobHarvester\0.1.0.158\JobHarvester.exe
    # (SHA-256 6ab25b77...); changed harvest.py, rebuilt at the same
    # 0.1.0.158, ran the new binary, re-hashed: 677d8878.... The launcher
    # compares the payload against what is on disk and re-extracts, so no
    # conditional "cached only at a tag" fallback is needed.
    #
    # The spec carries an extra "cache" level for a reason. {CACHE_DIR} on
    # Windows is %LOCALAPPDATA% and {COMPANY} is JobCrawler, which is exactly
    # the per-user data directory src/config/paths.py falls back to
    # (APP_NAME = "JobCrawler"), so the bare
    # {CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION} this started as put the
    # unpack tree beside jobs.db as JobHarvester\<version>\ on a fresh
    # install. Harmless today -- nothing walks that directory recursively,
    # the one DATA_DIR scan being profile.py's non-recursive resume glob --
    # but it invites a future recursive scan to walk 189 MB of unpacked
    # binary, and it makes the data directory unreadable to anyone opening
    # it. One more level keeps build output and user data apart
    # (2026-09-11).
    #
    # Known gap, deliberately not fixed here: every distinct {VERSION}
    # leaves its own folder under that cache directory and nothing prunes
    # the old ones, so a long series of builds accumulates unpack trees the
    # user has to delete by hand.
    cmd += ["--onefile-cache-mode=cached",
            "--onefile-tempdir-spec="
            "{CACHE_DIR}/{COMPANY}/cache/{PRODUCT}/{VERSION}"]
    # Our own package configuration, in Nuitka's format, for hidden
    # dependencies its bundled config does not describe (currently ddgs's
    # directory-walked engine registry). Both targets get it: the harvester
    # does not follow ddgs at all, so the entry simply never fires there,
    # and one flag for both keeps the file the single place those
    # declarations live.
    cmd += [f"--user-package-configuration-file={PACKAGE_CONFIG}"]
    # Only the UI gets the registry-derived includes, because only the UI
    # resolves a target string: registry.invoke() is called from
    # src/ops/background.py (the UI's op runner), run_scraper.py and
    # discover.py, and `python tools/entrydeps.py harvest.py --modules` lists
    # no src.ops.background -- out of src/ops it reaches only src.ops,
    # src.ops.maintenance and src.ops.registry. src.ops.registry IS in that
    # list, but only because src/ops/__init__.py re-exports it and every
    # `from src.ops import maintenance` therefore runs it; the harvester
    # imports src.crawl.harvest and calls it directly and never looks an
    # operation up by name. So the --include-module=src.ops.roster this
    # target used to carry was dead config: nothing in a whole-board pull
    # can reach roster, by import or by string.
    if name == "harvest":
        cmd += [f"--noinclude-custom-mode={p}:error" for p in HARVEST_FORBID]
        cmd += [f"--nofollow-import-to={p}" for p in HARVEST_LAZY]
        # Ctrl+C in the harvester's console reaches the compiled child as a
        # KeyboardInterrupt, and harvest.py's clean path then returns from
        # main() -- which waits on the harvest ThreadPoolExecutor's
        # non-daemon threads at interpreter exit. An in-flight board fetch
        # is bounded by config.policy.FETCH_TIMEOUT = (5.0, 25.0), i.e. up
        # to 30 s of connect + read. The onefile launcher's default grace
        # time is 5000 ms, so the default hard-killed the child mid-commit.
        # 35 s clears one whole request with margin.
        cmd += ["--onefile-child-grace-time=35000"]
        cmd += [f"--include-data-files={src}={dst}" for src, dst in DATA_FILES
                if not src.startswith("src/web/")]
    else:
        cmd += [f"--include-module={m}" for m in include_modules()]
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


def output_dir():
    """The --output-dir= the caller passed through to Nuitka, or ROOT.

    Nuitka writes the binary wherever that flag says, so the success check
    in main() has to look there too: without this a perfectly good build
    into a scratch directory was reported as "Build FAILED".
    """
    for a in sys.argv[1:]:
        if a.startswith("--output-dir="):
            return Path(a.split("=", 1)[1])
    return ROOT


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
    # (which --standalone creates). With --onefile it is ROOT, or wherever
    # --output-dir= sent it.
    out = output_dir() / OUTPUT_NAME
    if rc == 0 and out.exists():
        print(f"\nBuild OK: {out}")
    else:
        print(f"\nBuild FAILED - expected {out}")
        rc = rc or 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
