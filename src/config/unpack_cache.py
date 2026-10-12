r"""The compiled binaries' onefile unpack cache, and pruning the versions a
newer build has replaced.

build_app.py builds JobCrawlerUI.exe and JobHarvester.exe to unpack once per
version, into <root>\<product>\<version>\, where <root> is `cache_root()`:
%LOCALAPPDATA%\JobCrawler\cache
(tests/test_invariants.py::test_the_build_unpacks_where_unpack_cache_looks).
Each binary calls `prune_stale()` at startup, in harvest.py's and
src/web/server.py's main()
(::test_both_binaries_prune_their_unpack_cache_at_startup), to remove its own
product's folders for OLDER versions that nothing is using. The docstrings
below cite their tests in tests/test_unpack_cache.py.

Notes:
    Nuitka itself never deletes one of those folders, so before this every
    build left its own behind for good: a disk scan on 2026-10-09 found 19
    JobCrawlerUI and 20 JobHarvester folders, ~7 GB at ~120-300 MB each.

    Windows only, because "nothing is using it" is read off two Windows
    refusals: an image a running process has mapped cannot be opened for
    writing, and a folder with an open file under it cannot be renamed. A
    running exe or loaded DLL does NOT block the rename by itself (measured
    2026-10-09), which is why `in_use` exists rather than the rename alone.
    POSIX has neither refusal, so the macOS/Linux caches keep the old gap.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from src import config


def version_key(name: str) -> tuple[int, int, int, int] | None:
    """The four numbers of a folder named for a build_app.version(), or
    None for any other name.

    >>> version_key("0.1.0.350")
    (0, 1, 0, 350)

    The numbers compare as numbers, so .350 is newer than .36:

    >>> version_key("0.1.0.350") > version_key("0.1.0.36")
    True

    >>> version_key("0.1.0.350.pruning-42"), version_key("0.1.0"), version_key("v0.1.0.1")
    (None, None, None)
    """
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", name, re.ASCII)
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4])) if m else None


def cache_root() -> Path | None:
    r"""%LOCALAPPDATA%\JobCrawler\cache, the {CACHE_DIR}/{COMPANY}/cache of
    build_app.py's --onefile-tempdir-spec; None off Windows or with
    LOCALAPPDATA unset (test_cache_root).

    Notes:
        Nuitka expands {CACHE_DIR} from the Local AppData known folder,
        which is what LOCALAPPDATA holds. Should the two ever differ,
        prune() finds the running folder outside this root and does nothing.
    """
    if sys.platform != "win32" or config.SETTINGS.localappdata is None:
        return None
    return config.SETTINGS.localappdata / config.APP_NAME / "cache"


def in_use(folder: Path) -> bool:
    """True if a running process has an executable image (.exe, .dll, .pyd)
    under `folder` mapped, which Windows refuses to open for writing
    (test_a_running_copy_is_in_use). The probe opens and closes without
    writing.

    Notes:
        Any other refusal (a read-only file, say) also reads as in use:
        the mistake this can make is keeping a folder, never deleting one.
    """
    for f in folder.rglob("*"):
        if f.suffix.lower() not in (".exe", ".dll", ".pyd") or not f.is_file():
            continue
        try:
            with open(f, "r+b"):
                pass
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def prune(running: Path, root: Path) -> list[tuple[str, int]]:
    """Delete the unpack folders `running` supersedes, and return the
    version and size in bytes of each one removed, in name order.

    `running` is a binary's own folder, <root>/<product>/<version>. Anywhere
    else, or under a name that is no version, nothing is pruned
    (test_prune_needs_the_cache_layout). The candidates are that product's
    folders named for an OLDER version, so neither `running` nor a newer
    build's folder nor another product's is touched
    (test_prune_removes_older_versions_only), plus the leftovers of a prune
    that was interrupted (<version>.pruning-<pid>,
    test_an_interrupted_prune_is_finished). Other names, plain files,
    symlinks and junctions are left alone (test_prune_leaves_other_names_alone).

    A candidate is skipped while it is in use: a process runs an image from
    it (`in_use`), or a file under it is open, which makes Windows refuse
    the rename that moves it aside before deleting
    (test_an_open_file_keeps_its_folder).

    Notes:
        The rename is also what makes two instances pruning at once safe:
        only one of them can move a given folder. Moving it aside first
        means a delete cut short (the process exits mid-way) never leaves a
        half-empty folder under a version name a launcher would reuse.

        One race remains: an older build's launcher can start between
        in_use() and the rename, while it is still checking files, and lose
        the ones already checked. The child it starts then fails, and the
        next launch of that build re-extracts whatever is missing (Nuitka's
        cached mode rewrites any file whose CRC does not match).
    """
    running, root = running.resolve(), root.resolve()
    product, mine = running.parent, version_key(running.name)
    if mine is None or product.parent != root:
        return []
    removed: list[tuple[str, int]] = []
    for d in sorted(product.iterdir()):
        if d.is_symlink() or d.is_junction() or not d.is_dir():
            continue
        name, aside, pid = d.name.partition(".pruning-")
        key = version_key(name)
        if key is None or (aside and not pid.isdigit()):
            continue
        if aside:
            trash = d
        elif key < mine and not in_use(d):
            trash = d.with_name(f"{d.name}.pruning-{os.getpid()}")
            try:
                d.rename(trash)
            except OSError:      # a file under it is open, or another instance moved it
                continue
        else:
            continue
        try:
            size = sum(f.stat().st_size for f in trash.rglob("*") if f.is_file())
        except OSError:          # another instance is deleting it too
            size = 0
        shutil.rmtree(trash, ignore_errors=True)
        if not trash.exists():
            removed.append((name, size))
    return removed


def prune_stale() -> str | None:
    """prune() for the folder this compiled binary runs from, and a line
    saying what it freed; None when nothing was removed, and always when
    running from source or off Windows (test_prune_stale).

    Notes:
        A onefile child's sys.executable is <its unpack folder>/python.exe:
        Nuitka points it "inside the distribution" in standalone mode
        (nuitka/code_generation/templates/CodeTemplatesConstants.py, 4.2.2).
    """
    root = cache_root()
    if "__compiled__" not in globals() or root is None:
        return None
    running = Path(sys.executable).parent
    removed = prune(running, root)
    if not removed:
        return None
    freed = sum(size for _, size in removed) / 1e9
    return (f"pruned {len(removed)} old unpack folder(s) of {running.parent.name} "
            f"({freed:.1f} GB) from {root}")
