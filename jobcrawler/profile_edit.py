"""profile.toml editing for the web UI's Settings tab.

Pure functions over the profile file: read, validate, apply structured
updates (comment-preserving via tomlkit), and back-up-then-write atomically.
webapp.py wires these to /api/config* routes; nothing here touches Flask or
the live `config` module — edits take effect when the server restarts
(config.py snapshots the profile at import time, as do most consumers).
"""

import os
import shutil
import tomllib
from datetime import datetime

import tomlkit

import config

BACKUP_DIR_NAME = "config_backups"
BACKUP_KEEP = 20

# Sections whose direct list-valued keys must be lists of strings. Sub-tables
# (e.g. [keywords.local_tech]) are validated recursively with the same rule.
_STR_LIST_SECTIONS = ("keywords", "exclude", "locations", "locality",
                      "discovery")
_REQUIRED_SECTIONS = ("keywords", "locations", "locality")


def read_raw():
    """Return (text, source_filename) — profile.toml if present, else the
    checked-in example (mirrors config._load_profile)."""
    for p in (config.PROFILE_PATH, config.PROFILE_EXAMPLE_PATH):
        if p.exists():
            return p.read_text(encoding="utf-8"), p.name
    return "", None


def _check_str_lists(errors, name, table):
    for key, val in table.items():
        if isinstance(val, dict):
            _check_str_lists(errors, f"{name}.{key}", val)
        elif isinstance(val, list):
            # Arrays of tables (e.g. discovery.priority_companies) are fine;
            # the string rule applies only to plain keyword-style lists.
            if any(isinstance(x, dict) for x in val):
                continue
            if not all(isinstance(x, str) for x in val):
                errors.append(f"[{name}] {key} must be a list of strings")


def validate(text):
    """Validate profile TOML text. Returns a list of human-readable error
    strings; empty list = valid."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [f"TOML syntax error: {e}"]

    errors = []
    for sec in _REQUIRED_SECTIONS:
        if sec not in data:
            errors.append(f"missing required section [{sec}]")

    for sec in _STR_LIST_SECTIONS:
        tbl = data.get(sec)
        if isinstance(tbl, dict):
            _check_str_lists(errors, sec, tbl)

    fit = data.get("fit", {})
    for group in ("weights", "gate_penalty"):
        for k, v in (fit.get(group) or {}).items():
            if not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:
                errors.append(f"[fit] {group}.{k} must be a number in 0..1")

    for i, tier in enumerate(data.get("mission", {}).get("tiers") or []):
        band = tier.get("band")
        if (not isinstance(band, list) or len(band) != 2
                or not all(isinstance(b, (int, float)) for b in band)):
            errors.append(f"[mission] tiers[{i}].band must be [lo, hi]")

    for tid, t in (data.get("tracks") or {}).items():
        if isinstance(t, dict) and not str(t.get("db") or "").strip():
            errors.append(f"[tracks.{tid}] db must be a non-empty filename")

    return errors


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
        bdir = target.parent / BACKUP_DIR_NAME
        bdir.mkdir(exist_ok=True)
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
