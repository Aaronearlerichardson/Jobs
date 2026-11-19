#!/usr/bin/env python3
"""Install a Nuitka that compiles for free-threaded CPython 3.14t.

    python tools/patched_nuitka.py

Stock Nuitka fails in its C runtime on 3.14t (Nuitka/Nuitka#4060, open).
kpip carries a patch that fixes that and makes compiled code safe when
threads share it (warpforgedotco/kpip#202). This applies that patch, pinned
by commit and hash, to the Nuitka commit it was written against, and
pip-installs the result over whatever Nuitka this interpreter has. Needs git.
Drop it for a stock release once one builds 3.14t.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

NUITKA_REPO = "https://github.com/Nuitka/Nuitka.git"
NUITKA_COMMIT = "203ccb23d2e77a409597cdbab159f6e7984c9721"   # develop 2026-10-05, 4.3rc6
PATCH_URL = ("https://raw.githubusercontent.com/warpforgedotco/kpip/"
             "f3bd2575240d2b5c9f44f3b0b07720ace48de3a8/"
             "tools/vendoring/patches/nuitka/0015-free-threaded-python.patch")
PATCH_SHA256 = "a88ab25584c86fb5fbed7dba8bf17ad7909a951e4c3342047ef62635f6006320"


def main() -> int:
    with urllib.request.urlopen(PATCH_URL, timeout=60) as r:
        patch = r.read()
    digest = hashlib.sha256(patch).hexdigest()
    if digest != PATCH_SHA256:
        sys.exit(f"patch hash {digest} is not the reviewed {PATCH_SHA256}; refusing")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "nuitka")
        src.mkdir()
        (Path(tmp) / "ft.patch").write_bytes(patch)
        for cmd in (["git", "init", "-q"],
                    ["git", "fetch", "-q", "--depth", "1", NUITKA_REPO, NUITKA_COMMIT],
                    ["git", "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"],
                    ["git", "apply", str(Path(tmp) / "ft.patch")]):
            subprocess.run(cmd, cwd=src, check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", str(src)], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
