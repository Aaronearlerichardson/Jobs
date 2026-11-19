"""Fail when a Nuitka inclusion report names a type checker.

Usage: python tools/check_no_typechecker.py REPORT.xml [...]

Matches on the module's distribution, not its name: chardet and
charset_normalizer ship legitimate *__mypyc runtime modules, while a type
checker's own hash-named blob is attributed to its distribution.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

CHECKERS = {"mypy", "pyrefly"}


def main(reports: list[str]) -> int:
    hits = sorted({m.get("name") or "" for f in reports
                   for m in ET.parse(f).getroot().iter("module")
                   if m.get("distribution") in CHECKERS})
    if hits:
        print(f"type checker in the binary: {hits[:10]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
