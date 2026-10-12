"""src/config/unpack_cache.py: pruning the onefile unpack folders a newer
build has replaced.

Every test builds its own cache tree under tmp_path and hands prune() that
root, so nothing here goes near the real %LOCALAPPDATA%\\JobCrawler\\cache.
The Windows-only tests run a real process from a folder, or hold a real
file open in one: "in use" is read off Windows' own refusals, and a mock of
those would prove nothing.
"""

import os
import shutil
import subprocess
import sys

import pytest

from src import config
from src.config import unpack_cache

windows = pytest.mark.skipif(sys.platform != "win32",
                             reason="the in-use checks are Windows refusals")


def unpack(root, product, version, size=1000):
    """A stand-in unpack folder of `size` bytes: the product's exe at the
    top, a data file one level down, as Nuitka lays them out."""
    d = root / product / version
    (d / "src").mkdir(parents=True)
    (d / f"{product}.exe").write_bytes(b"MZ")
    (d / "src" / "index.html").write_bytes(b"x" * (size - 2))
    return d


def names(product_dir):
    return sorted(p.name for p in product_dir.iterdir())


def test_prune_removes_older_versions_only(tmp_path):
    root = tmp_path / "cache"
    old = [unpack(root, "JobCrawlerUI", v) for v in ("0.1.0.198", "0.1.0.36")]
    running = unpack(root, "JobCrawlerUI", "0.1.0.350")
    newer = unpack(root, "JobCrawlerUI", "0.1.0.351")
    other = unpack(root, "JobHarvester", "0.1.0.1")

    # .36 is older than .350 by number, though it sorts after it by name.
    assert unpack_cache.prune(running, root) == [("0.1.0.198", 1000),
                                                 ("0.1.0.36", 1000)]
    assert not any(d.exists() for d in old)
    assert names(root / "JobCrawlerUI") == ["0.1.0.350", "0.1.0.351"]
    assert all((d / "src" / "index.html").exists() for d in (running, newer, other))


@pytest.mark.parametrize("running", [
    "elsewhere/JobCrawlerUI/0.1.0.350",      # not under the cache root
    "cache/0.1.0.350",                       # no product level
    "cache/x/JobCrawlerUI/0.1.0.350",        # one level too deep
    "cache/JobCrawlerUI/onefile_1234_5678",  # Nuitka's default temp naming
])
def test_prune_needs_the_cache_layout(tmp_path, running):
    root = tmp_path / "cache"
    running = tmp_path / running
    running.mkdir(parents=True)
    sibling = unpack(running.parent.parent, running.parent.name, "0.1.0.1")

    assert unpack_cache.prune(running, root) == []
    assert (sibling / "src" / "index.html").exists()


def test_prune_leaves_other_names_alone(tmp_path):
    root = tmp_path / "cache"
    running = unpack(root, "JobCrawlerUI", "0.1.0.350")
    product = running.parent
    for name in ("notes", "0.1.0", "0.1.0.1.bak", "0.1.0.2.pruning-x"):
        (product / name).mkdir()
    (product / "0.1.0.3").write_text("a file, not a folder")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the cache's")
    link = product / "0.1.0.4"
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)

    assert unpack_cache.prune(running, root) == []
    assert names(product) == ["0.1.0", "0.1.0.1.bak", "0.1.0.2.pruning-x",
                              "0.1.0.3", "0.1.0.350", "0.1.0.4", "notes"]
    assert (outside / "keep.txt").read_text() == "not the cache's"


def test_an_interrupted_prune_is_finished(tmp_path):
    """A folder already moved aside is deleted whatever its version: no
    launcher can reach it under that name."""
    root = tmp_path / "cache"
    running = unpack(root, "JobCrawlerUI", "0.1.0.350")
    unpack(root, "JobCrawlerUI", "0.1.0.1.pruning-7")
    unpack(root, "JobCrawlerUI", "0.1.0.400.pruning-123")

    assert unpack_cache.prune(running, root) == [("0.1.0.1", 1000),
                                                 ("0.1.0.400", 1000)]
    assert names(root / "JobCrawlerUI") == ["0.1.0.350"]


@windows
def test_a_running_copy_is_in_use(tmp_path):
    root = tmp_path / "cache"
    running = unpack(root, "JobCrawlerUI", "0.1.0.350")
    old = unpack(root, "JobCrawlerUI", "0.1.0.1")
    exe = old / "JobCrawlerUI.exe"
    # A real exe that sits waiting on stdin, offline, standing in for an
    # older build that is still running from its folder.
    shutil.copy(os.environ["COMSPEC"], exe)
    before = exe.read_bytes(), exe.stat().st_mtime_ns

    p = subprocess.Popen([str(exe), "/q", "/k"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert unpack_cache.in_use(old)
        assert unpack_cache.prune(running, root) == []
        assert names(root / "JobCrawlerUI") == ["0.1.0.1", "0.1.0.350"]
    finally:
        p.stdin.close()
        p.wait(timeout=10)

    assert (exe.read_bytes(), exe.stat().st_mtime_ns) == before, "the probe wrote"
    assert not unpack_cache.in_use(old)
    assert [v for v, _ in unpack_cache.prune(running, root)] == ["0.1.0.1"]
    assert not old.exists()


@windows
def test_an_open_file_keeps_its_folder(tmp_path):
    """A file open under a folder (a launcher checking or extracting it)
    maps no image, so in_use() passes it; the rename is what refuses."""
    root = tmp_path / "cache"
    running = unpack(root, "JobCrawlerUI", "0.1.0.350")
    old = unpack(root, "JobCrawlerUI", "0.1.0.1")

    with open(old / "src" / "index.html", "rb"):
        assert not unpack_cache.in_use(old)
        assert unpack_cache.prune(running, root) == []
        assert names(root / "JobCrawlerUI") == ["0.1.0.1", "0.1.0.350"]

    assert [v for v, _ in unpack_cache.prune(running, root)] == ["0.1.0.1"]


def test_cache_root(monkeypatch, tmp_path):
    monkeypatch.setattr(config.SETTINGS, "localappdata", tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    assert unpack_cache.cache_root() == tmp_path / "JobCrawler" / "cache"

    monkeypatch.setattr(config.SETTINGS, "localappdata", None)
    assert unpack_cache.cache_root() is None

    monkeypatch.setattr(config.SETTINGS, "localappdata", tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    assert unpack_cache.cache_root() is None


def test_prune_stale(monkeypatch, tmp_path):
    # From source it never prunes, whatever the machine's real cache holds.
    assert unpack_cache.prune_stale() is None

    root = tmp_path / "JobCrawler" / "cache"
    running = unpack(root, "JobHarvester", "0.1.0.350")
    old = unpack(root, "JobHarvester", "0.1.0.1")
    monkeypatch.setattr(unpack_cache, "__compiled__", True, raising=False)
    monkeypatch.setattr(config.SETTINGS, "localappdata", tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(running / "python.exe"))

    assert unpack_cache.prune_stale() == (
        f"pruned 1 old unpack folder(s) of JobHarvester (0.0 GB) from {root}")
    assert not old.exists() and running.exists()
    assert unpack_cache.prune_stale() is None          # nothing left to prune

    again = unpack(root, "JobHarvester", "0.1.0.2")
    monkeypatch.setattr(sys, "platform", "linux")
    assert unpack_cache.prune_stale() is None
    assert again.exists()
