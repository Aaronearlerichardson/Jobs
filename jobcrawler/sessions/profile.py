"""
Chrome real-profile copy + session-state reset.

When Cloudflare Turnstile keeps failing even with stealth, escalation is
to use the user's REAL Chrome profile (cookies, history, extensions).
Chrome refuses to start CDP on its default user-data dir, so we maintain
a copy that CDP can drive.
"""

import json
import os
import platform
import shutil
import sys
from pathlib import Path

from config import PROFILE_COPY_DIR


def default_chrome_user_data_dir():
    """Best-effort autodetect of the Chrome user-data directory per OS."""
    sysname = platform.system()
    if sysname == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return str(Path(local) / "Google" / "Chrome" / "User Data")
    elif sysname == "Darwin":
        return str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome")
    else:
        return str(Path.home() / ".config" / "google-chrome")
    return None


def prepare_profile_copy(*, refresh=False):
    """
    Copy the user's real Chrome profile to a non-default location so CDP
    works. Skips cache directories (fast) but preserves cookies/history/
    Local Storage/Login Data (what Cloudflare/LinkedIn care about).
    Returns the path to the copy.
    """
    from . import runtime

    src = runtime.user_data_dir or default_chrome_user_data_dir()
    if not src:
        print("  [!] Can't autodetect Chrome profile. "
              "Pass --user-data-dir explicitly.")
        sys.exit(1)

    src_path = Path(src)
    if not src_path.exists():
        print(f"  [!] Chrome profile not found at: {src}")
        print(f"      Pass --user-data-dir to point at the right location.")
        sys.exit(1)

    PROFILE_COPY_DIR.parent.mkdir(parents=True, exist_ok=True)

    if PROFILE_COPY_DIR.exists() and not refresh:
        print(f"  Reusing profile copy at {PROFILE_COPY_DIR}")
        print("  (Pass --refresh-profile to re-copy from your live profile.)")
        return str(PROFILE_COPY_DIR)

    if PROFILE_COPY_DIR.exists():
        print("  Removing old profile copy...")
        shutil.rmtree(PROFILE_COPY_DIR, ignore_errors=True)

    print(f"\n  Copying your Chrome profile -> {PROFILE_COPY_DIR}")
    print(f"  (Preserves cookies/history/login data. May take 10-60s.)")
    print(f"  Chrome must be CLOSED or some files will be locked.\n")

    skip_dir_names = {
        "cache", "code cache", "gpucache", "dawncache", "shadercache",
        "service worker", "subresource filter", "optimization guide",
        "file system", "indexeddb", "webstorage",
        "crashpad", "guestprofile", "system profile",
    }

    def _ignore(dirpath, entries):
        out = set()
        for e in entries:
            if e.lower() in skip_dir_names:
                out.add(e)
            if e in ("SingletonCookie", "SingletonLock", "SingletonSocket",
                     "lockfile", "LOCK"):
                out.add(e)
        return out

    try:
        shutil.copytree(src_path, PROFILE_COPY_DIR, ignore=_ignore,
                        dirs_exist_ok=True)
    except PermissionError as e:
        print(f"  [!] Copy failed - file locked: {e}")
        print(f"      Chrome is still running. Quit it fully and retry.")
        sys.exit(1)
    except Exception as e:
        print(f"  [!] Partial copy (continuing anyway): {e}")

    return str(PROFILE_COPY_DIR)


def clear_chrome_locks(user_dir):
    """
    Remove Singleton locks Chrome leaves in a user-data dir after exit.
    If these are present at launch, Chrome forwards the launch request to
    a nonexistent sibling and dies — Playwright sees "Browser window not
    found". Safe to delete — they're just cooperative locks.
    """
    lock_names = [
        "SingletonCookie", "SingletonLock", "SingletonSocket",
        "lockfile", "LOCK",
    ]
    base = Path(user_dir)
    removed = []
    for name in lock_names:
        for p in base.glob(name):
            try:
                p.unlink()
                removed.append(p.name)
            except Exception:
                pass
        try:
            for p in base.glob(f"{name}*"):
                if p.is_symlink():
                    p.unlink()
                    removed.append(p.name)
        except Exception:
            pass
    if removed:
        print(f"  Cleared stale Chrome lock files: "
              f"{', '.join(sorted(set(removed)))}")


def reset_chrome_session_state(user_dir, profile_dir="Default"):
    """
    Reset per-profile state Chrome writes on exit, so the next launch
    starts cleanly. Without this, the 2nd launch of a reused copy fails
    with "Browser window not found" (Chrome enters recovery that doesn't
    play with CDP control).

      1. Patch Preferences so exited_cleanly=true, exit_type=Normal.
      2. Delete Last/Current Session/Tabs files.
    """
    profile_path = Path(user_dir) / profile_dir
    if not profile_path.exists():
        return

    pref_file = profile_path / "Preferences"
    if pref_file.exists():
        try:
            prefs = json.loads(pref_file.read_text(encoding="utf-8"))
            prof = prefs.setdefault("profile", {})
            changed = False
            if prof.get("exited_cleanly") is not True:
                prof["exited_cleanly"] = True
                changed = True
            if prof.get("exit_type") != "Normal":
                prof["exit_type"] = "Normal"
                changed = True
            if changed:
                pref_file.write_text(json.dumps(prefs), encoding="utf-8")
        except Exception as e:
            print(f"  [warn] Couldn't patch Preferences ({e}). Continuing.")

    stale_files = ["Last Session", "Last Tabs", "Current Session", "Current Tabs"]
    for name in stale_files:
        p = profile_path / name
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    sessions_dir = profile_path / "Sessions"
    if sessions_dir.exists():
        try:
            shutil.rmtree(sessions_dir, ignore_errors=True)
        except Exception:
            pass
