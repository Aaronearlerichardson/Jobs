"""profile.toml editing for the web UI's Settings tab.

Pure functions over the profile file: read, validate, apply structured
updates (comment-preserving via tomlkit), and back-up-then-write atomically.
webapp.py wires these to /api/config* routes; nothing here touches Flask or
the live `config` module — edits take effect when the server restarts
(the config package snapshots the profile at import time, as do most consumers).
"""

import os
import shutil
import tomllib
from datetime import datetime

import tomlkit

from src import config
from .profile_schema import problems

BACKUP_DIR_NAME = "config_backups"
BACKUP_KEEP = 20


def read_raw():
    """Return (text, source_filename) — profile.toml if present, else the
    checked-in example (mirrors config._load_profile)."""
    for p in (config.PROFILE_PATH, config.PROFILE_EXAMPLE_PATH):
        if p.exists():
            return p.read_text(encoding="utf-8"), p.name
    return "", None


def validate(text):
    """Profile TOML text checked against the profile schema, the same one
    the loader applies: one 'path: problem' string per bad key (see
    profile_schema.problems), [] when valid."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [f"TOML syntax error: {e}"]
    return problems(data)


def apply_updates(updates):
    """Apply {dotted.path: value} updates to the profile with tomlkit
    (comments/order preserved) and return the new TOML text. Creates the
    document from the example template first when the user is still on the
    profile.example.toml fallback. Intermediate tables are created as needed.
    """
    raw, source = read_raw()
    doc = tomlkit.parse(raw)
    for path, value in updates.items():
        keys = [k for k in str(path).split(".") if k]
        if not keys:
            continue
        node = doc
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], (dict, tomlkit.items.Table)):
                node[k] = tomlkit.table()
            node = node[k]
        node[keys[-1]] = value
    return tomlkit.dumps(doc)


def backup_then_write(text):
    """Back up the current profile.toml (timestamped, last BACKUP_KEEP kept),
    then atomically replace it with `text`. Returns the backup path (or None
    when there was nothing to back up)."""
    target = config.PROFILE_PATH
    backup = None
    if target.exists():
        bdir = config.DATA_DIR / BACKUP_DIR_NAME
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = bdir / f"profile-{stamp}.toml"
        shutil.copy2(target, backup)
        old = sorted(bdir.glob("profile-*.toml"))
        for p in old[:-BACKUP_KEEP]:
            try:
                p.unlink()
            except OSError:
                pass
    tmp = target.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return backup
