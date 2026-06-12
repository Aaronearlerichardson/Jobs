"""
Write confirmed discovery candidates back into config.py in place.

Strategy: regex-scan config.py for the target dict/list literal
(GREENHOUSE_COMPANIES etc.), balance-match its brackets, and insert new
entries right before the closing bracket - tagged with a date/term
comment so you can audit or revert. Slug-level dedup means re-running
is safe.

Why regex and not ast? The AST parser strips comments, whitespace, and
trailing commas, so writing back would require a full source-preserving
unparser. This file is simple enough (one dict/list per target) that
balance-matching brackets gets the job done with zero risk of
clobbering the rest of config.py.
"""

from datetime import datetime
from pathlib import Path

import re

from config import SCRIPT_DIR

CONFIG_PATH: Path = SCRIPT_DIR / "config.py"

# ATS name  ->  (config variable, container kind)
#   "dict"            -> {slug: "Company"}
#   "list_name_slug"  -> [("Company", slug), ...]
#   "list_workday"    -> [(tenant, wd_pod_int, site, "Company"), ...]
TARGETS: dict[str, tuple[str, str]] = {
    "greenhouse": ("GREENHOUSE_COMPANIES", "dict"),
    "lever":      ("LEVER_COMPANIES",      "dict"),
    "ashby":      ("ASHBY_COMPANIES",      "dict"),
    "kula":       ("KULA_COMPANIES",       "list_name_slug"),
    "workday":    ("WORKDAY_COMPANIES",    "list_workday"),
}


def _find_block(src: str, var_name: str):
    """
    Locate `VAR_NAME[: type] = { ... }` or `VAR_NAME[: type] = [ ... ]`.
    Returns (start, end, body) where `end` is the index of the closing
    bracket, or None if the variable isn't found.
    """
    open_pat = re.compile(
        rf"^{re.escape(var_name)}(?::\s*[^\n=]+)?\s*=\s*([\{{\[])",
        re.M,
    )
    m = open_pat.search(src)
    if not m:
        return None
    open_ch  = m.group(1)
    close_ch = "}" if open_ch == "{" else "]"

    i = m.end()              # first index AFTER the opening bracket
    depth  = 1
    in_str = None            # current opening quote char, or None
    while i < len(src) and depth > 0:
        ch = src[i]
        if in_str:
            if ch == "\\":           # skip escaped char
                i += 2
                continue
            if ch == in_str:
                in_str = None
        else:
            if ch in ("'", '"'):
                in_str = ch
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return m.start(), i, src[m.end():i]
        i += 1
    return None


# Keys in a dict literal look like:  "slug": "Name",
_DICT_KEY_RE = re.compile(r'''["']([^"']+)["']\s*:''')
# Tuple in KULA list:  ("Name", "slug")
_TUPLE_SLUG_RE = re.compile(r'''\(\s*["'][^"']+["']\s*,\s*["']([^"']+)["']''')
# 4-tuple in WORKDAY_COMPANIES:  ("tenant", 5, "site", "Name")
_TUPLE_WORKDAY_RE = re.compile(
    r'''\(\s*["']([^"']+)["']\s*,\s*(\d+)\s*,\s*["']([^"']+)["']\s*,\s*["'][^"']+["']\s*\)'''
)


def _existing_slugs_dict(body: str) -> set[str]:
    return set(_DICT_KEY_RE.findall(body))


def _existing_slugs_list(body: str) -> set[str]:
    return set(_TUPLE_SLUG_RE.findall(body))


def _existing_slugs_workday(body: str) -> set[str]:
    """Dedup key matches the `tenant|pod|site` format in Candidate.slug_guess."""
    return {f"{t}|{p}|{s}" for t, p, s in _TUPLE_WORKDAY_RE.findall(body)}


def _verify_suffix(cand) -> str:
    """Carry a VERIFY flag from discovery into the config comment."""
    m = re.search(r"\[VERIFY: ([^\]]+)\]", getattr(cand, "notes", "") or "")
    return f" — VERIFY: {m.group(1)}" if m else ""


def _fmt_dict_entry(cand) -> str:
    return (f'    "{cand.slug_guess}": "{cand.name}",  '
            f'# {cand.job_count} job(s), discovered{_verify_suffix(cand)}')


def _fmt_list_entry(cand) -> str:
    return (f'    ("{cand.name}", "{cand.slug_guess}"),  '
            f'# {cand.job_count} job(s), discovered{_verify_suffix(cand)}')


def _fmt_workday_entry(cand):
    """
    Workday slug_guess is "tenant|wd_pod|site". Emits the 4-tuple shape
    WORKDAY_COMPANIES expects. Returns None if the slug is malformed —
    the caller skips those.
    """
    parts = (cand.slug_guess or "").split("|")
    if len(parts) != 3:
        return None
    tenant, pod, site = parts
    if not pod.isdigit():
        return None
    return (f'    ("{tenant}", {int(pod)}, "{site}", "{cand.name}"),  '
            f'# {cand.job_count} job(s), discovered{_verify_suffix(cand)}')


def apply_to_config(result, dry_run: bool = False) -> list[str]:
    """
    Insert confirmed candidates into config.py; return summary lines.
    When `dry_run=True`, nothing is written.
    """
    term = result["term"]
    confirmed = [c for c in result["companies"] if c.confirmed]
    if not confirmed:
        return [f"  (no confirmed candidates for '{term}')"]

    by_ats: dict[str, list] = {}
    for c in confirmed:
        by_ats.setdefault(c.ats, []).append(c)

    src = CONFIG_PATH.read_text(encoding="utf-8")
    original_src = src
    summary: list[str] = []
    total_added   = 0
    total_skipped = 0
    date_str = datetime.now().strftime("%Y-%m-%d")

    for ats_name, cands in by_ats.items():
        target = TARGETS.get(ats_name)
        if not target:
            summary.append(f"    [skip] no config dict for ATS '{ats_name}'")
            total_skipped += len(cands)
            continue
        var_name, kind = target

        block = _find_block(src, var_name)
        if not block:
            summary.append(f"    [skip] {var_name} not found in config.py")
            total_skipped += len(cands)
            continue
        _, end, body = block

        if kind == "dict":
            existing = _existing_slugs_dict(body)
            fmt      = _fmt_dict_entry
        elif kind == "list_workday":
            existing = _existing_slugs_workday(body)
            fmt      = _fmt_workday_entry
        else:
            existing = _existing_slugs_list(body)
            fmt      = _fmt_list_entry

        new_lines, skipped = [], []
        for c in cands:
            slug = (c.slug_guess or "").strip()
            if not slug:
                continue
            if slug in existing:
                skipped.append(c.name)
                continue
            entry = fmt(c)
            if entry is None:     # fmt can reject malformed workday slugs
                continue
            new_lines.append(entry)
            existing.add(slug)

        if not new_lines:
            summary.append(f"    {var_name}: nothing to add "
                           f"({len(skipped)} already present)")
            total_skipped += len(skipped)
            continue

        header = f"    # --- discovered {date_str}: {term} ---"
        # Normalize: ensure exactly one newline separates prior content
        # from our insertion, and one newline before the closing bracket.
        prefix = "" if body.endswith("\n") else "\n"
        insertion = prefix + header + "\n" + "\n".join(new_lines) + "\n"
        src = src[:end] + insertion + src[end:]

        summary.append(
            f"    {var_name}: +{len(new_lines)} new, "
            f"{len(skipped)} duplicate(s) skipped"
        )
        for line in new_lines:
            summary.append(f"        {line.strip()}")
        total_added   += len(new_lines)
        total_skipped += len(skipped)

    header_line = (
        f"  [DRY-RUN] would add {total_added} entries, skip {total_skipped}"
        if dry_run
        else f"  + Applied: {total_added} new entr(ies), {total_skipped} skipped"
    )
    summary.insert(0, header_line)

    if not dry_run and total_added > 0:
        CONFIG_PATH.write_text(src, encoding="utf-8")
        summary.append(f"  Updated {CONFIG_PATH}")
    elif not dry_run and total_added == 0:
        # No write; leave the file untouched.
        assert src == original_src
    return summary
