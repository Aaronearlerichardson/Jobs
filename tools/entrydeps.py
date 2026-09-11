#!/usr/bin/env python3
"""What each entry point actually pulls in — the dependency DAG per binary.

    python tools/entrydeps.py                     # every entry point
    python tools/entrydeps.py harvest.py          # just one
    python tools/entrydeps.py --modules harvest.py   # name every module
    python tools/entrydeps.py --dot | dot -Tsvg -o deps.svg
    python tools/entrydeps.py --mermaid           # pasteable into markdown
    python tools/entrydeps.py --check             # vs build_app.py's skips

Answers the question a `--nofollow-import-to` list exists to answer: which
of YOUR modules does JobHarvester.exe have to contain, which does
JobCrawlerUI.exe, and what is only in one of them.

Uses the standard library's `modulefinder`, which reads compiled bytecode
rather than the import lines. Three things make that the right tool here
and a hand-rolled AST walk the wrong one:

  * It follows imports inside FUNCTION BODIES. 71 of the harvester's 86
    modules are reachable only that way -- harvest.py defers almost
    everything into main() -- and Nuitka follows them too, so a tool that
    reads only module-level imports reports the harvester as depending on
    nearly nothing.
  * It accounts for package __init__ execution. Importing
    src.discovery.local_sourcing runs src/discovery/__init__.py, which
    imports apply, bciwiki and pipeline: five modules nobody named. That
    is the mechanism that makes one small-looking import expensive.
  * It is what pydeps uses underneath, so this agrees with
    `pydeps <entry> --only src --max-bacon=0` exactly (86 = 86, verified).

If you want the picture rather than the numbers, pydeps draws it -- but
pass `--max-bacon=0`. Its default of 2 truncates by distance from the
entry point and reports 26 of the harvester's 86 modules.
"""

import argparse
import sys
from collections import Counter, defaultdict
from modulefinder import ModuleFinder
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools._harness import console_utf8                    # noqa: E402

console_utf8()

#: Root scripts worth mapping. The two that get compiled come first.
ENTRIES = ["harvest.py", "webapp.py", "run_scraper.py", "discover.py",
           "capture.py"]

#: Which binary each compiled entry becomes, for the report.
BINARY = {"harvest.py": "JobHarvester.exe", "webapp.py": "JobCrawlerUI.exe"}


class _Finder(ModuleFinder):
    """ModuleFinder that survives a namespace package.

    modulefinder predates PEP 420 and assumes every spec has a loader; a
    namespace package's is None, and it dies with `'NoneType' object has
    no attribute 'is_package'`. webapp.py hits one the moment it reaches
    flask. A namespace package has no code of its own to scan, so
    reporting it as not-found loses nothing -- `_safe_import_hook` already
    catches ImportError and files it under badmodules.
    """

    def find_module(self, name, path, parent=None):
        try:
            return super().find_module(name, path, parent)
        except AttributeError:
            raise ImportError(f"{name} is a namespace package") from None


def reachable(entry):
    """Every first-party module `entry` can reach, as a set of dotted names.

    modulefinder imports nothing and runs nothing: it walks compiled code
    objects. A script whose import fails still reports what it could see.
    """
    finder = _Finder(path=[str(ROOT)] + sys.path)
    finder.run_script(str(ROOT / entry))
    return {m for m in finder.modules if m == "src" or m.startswith("src.")}


def package_of(dotted):
    """src.ats.fetchers.getro -> ats. Bare `src` is the package __init__."""
    parts = dotted.split(".")
    return parts[1] if len(parts) > 1 else "(src root)"


def report(sets, show_modules=False):
    """Per entry: how many modules, broken down by package."""
    for entry, mods in sets.items():
        binary = BINARY.get(entry)
        print(f"\n{entry}" + (f"  ->  {binary}" if binary else ""))
        print(f"  {len(mods)} src module(s) reachable — what a build must "
              f"contain unless --nofollow-import-to excludes it")
        for pkg, n in sorted(Counter(package_of(m) for m in mods).items()):
            print(f"    src/{pkg:<14} {n:>3}")
        if show_modules:
            for m in sorted(mods):
                print(f"      {m}")


def compare(sets):
    """What the entry points share, and what each one alone drags in."""
    if len(sets) < 2:
        return
    shared = set.intersection(*(set(v) for v in sets.values()))
    print(f"\nshared by all {len(sets)} entry point(s): {len(shared)} module(s)")
    for entry, mods in sets.items():
        others = set().union(*(v for k, v in sets.items() if k != entry))
        only = sorted(m.replace("src.", "") for m in set(mods) - others)
        print(f"  only {entry:<16} {len(only):>2}: {', '.join(only) or '-'}")


def check_build(sets):
    """The harvester's reachable set against build_app.py's skip list.

    A skip that names something the entry cannot reach anyway is harmless
    but is not what is keeping the binary small — worth knowing which is
    which before trimming.
    """
    argv, sys.argv = sys.argv, [sys.argv[0]]
    try:
        import build_app
    finally:
        sys.argv = argv
    print(f"\nbuild_app.py HARVEST_SKIP = {build_app.HARVEST_SKIP}")
    reached = sets.get("harvest.py", set())
    for skip in build_app.HARVEST_SKIP:
        if not skip.startswith("src"):
            print(f"  {skip:<18} third-party — not checked here")
            continue
        hit = sorted(m for m in reached if m == skip or m.startswith(skip + "."))
        print(f"  {skip:<18} " + (f"REACHED via {', '.join(hit)} — this skip "
                                  f"is doing real work"
                                  if hit else
                                  "not reachable anyway — belt and braces"))


def graph(sets, mermaid=False):
    """A PACKAGE-level DAG: 13 nodes instead of 86, which is the difference
    between a picture and a hairball."""
    edges, packages = set(), defaultdict(set)
    for entry, mods in sets.items():
        for m in mods:
            packages[entry].add(package_of(m))
    for entry, pkgs in packages.items():
        for p in sorted(pkgs):
            edges.add((Path(entry).stem, p))
    if mermaid:
        print("graph LR")
        for a, b in sorted(edges):
            print(f"  {a} --> {b.replace('(', '').replace(')', '').replace(' ', '_')}")
        return
    print("digraph entrydeps {")
    print("  rankdir=LR; node [shape=box, fontname=Helvetica, fontsize=10];")
    everywhere = set.intersection(*(v for v in packages.values())) \
        if len(packages) > 1 else set()
    for entry in packages:
        print(f'  "{Path(entry).stem}" [shape=component, style=filled, '
              f'fillcolor="#dddddd"];')
    for p in sorted(set().union(*packages.values())):
        shade = "#eeeeee" if p in everywhere else "#cde5ff"
        print(f'  "{p}" [style=filled, fillcolor="{shade}"];')
    for a, b in sorted(edges):
        print(f'  "{a}" -> "{b}";')
    print("}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entries", nargs="*", default=None,
                    help="entry scripts (default: every launcher)")
    ap.add_argument("--modules", action="store_true",
                    help="list every reachable module, not just the counts")
    ap.add_argument("--dot", action="store_true", help="Graphviz, package level")
    ap.add_argument("--mermaid", action="store_true", help="Mermaid, package level")
    ap.add_argument("--check", action="store_true",
                    help="compare the harvester's set with build_app.py")
    args = ap.parse_args()

    wanted = args.entries or ENTRIES
    missing = [e for e in wanted if not (ROOT / e).exists()]
    if missing:
        print(f"  [!] no such entry script: {', '.join(missing)}")
        return 2
    sets = {e: reachable(e) for e in wanted}

    if args.dot or args.mermaid:
        graph(sets, mermaid=args.mermaid)
        return 0
    report(sets, show_modules=args.modules)
    compare(sets)
    if args.check:
        check_build(sets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
