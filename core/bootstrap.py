"""
First-run setup: give a fresh clone a working, private place to live.

The repository ships code and ONE example profile. Everything that is yours —
the job store, your résumé, your profile.toml, digests, caches — belongs on
your machine, not in the checkout, so that `git pull` never touches it and a
re-clone never inherits someone else's search. config.DATA_DIR is that place
(see config._resolve_data_dir).

The only thing missing on a fresh clone is a writable profile: without one
the app runs read-only off profile.example.toml and the Settings tab would
have nothing to save to. `ensure_profile()` copies the example across on the
first run and says where it went.
"""

import shutil

import config


def ensure_profile(announce=True):
    """Seed config.PROFILE_PATH from the bundled example if it doesn't exist.

    Returns True if a profile was created (first run), False if one was
    already there. Never overwrites, and never fails the caller: a read-only
    data dir just means the app keeps running off the example.
    """
    dest = config.PROFILE_PATH
    if dest.exists():
        return False
    src = config.PROFILE_EXAMPLE_PATH
    if not src.exists():
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    except OSError as e:
        if announce:
            print(f"  [!] Could not create {dest} ({e}) — "
                  f"running off {src.name}.")
        return False
    if announce:
        print(f"\n  Welcome. Created your profile at:\n    {dest}\n"
              f"  Your data (store, résumé, reports) lives in:\n"
              f"    {config.DATA_DIR}\n"
              f"  Nothing personal is written into the repository. Edit the "
              f"profile to describe\n  your search — by hand, or in the web "
              f"UI's Settings tab (python webapp.py).\n")
    return True


def status_lines():
    """Two lines naming where this install reads its settings and data from.

    Worth printing on every run: the #1 confusion with a tool that has both a
    checkout and a per-user data dir is not knowing which store you just
    crawled into.
    """
    return [f"profile : {config.PROFILE_PATH}"
            + ("" if config.PROFILE_PATH.exists()
               else f"  (missing — using {config.PROFILE_EXAMPLE_PATH.name})"),
            f"data    : {config.DATA_DIR}"]
