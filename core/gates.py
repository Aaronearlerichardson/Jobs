"""Config-driven posting gates.

The last two pieces of track behavior that used to live as code in the track
modules — the technical-title regex and the exclude tables — resolved
per-track from configuration instead:

  * `is_technical_role(title, t)` compiles `t["tech_title_regex"]`
    (profile.toml [tracks.*], engine defaults in config.py) — the cheap
    positive gate that keeps nurses/sales/admin titles away from any LLM
    scoring spend.
  * `exclude_reason(..., track_id=...)` reads the [exclude.<track_id>]
    tables (config.EXCLUDE_BY_TRACK) at call time — no hardcoded track key,
    so any user-defined track gets its own exclusion vocabulary. An absent
    or empty table makes the gate a no-op.
"""

import re
from functools import lru_cache

import config

from .filters import scrub_boilerplate

# Short/ambiguous tokens (<= 3 chars) use word-boundary matching so "sdr"
# doesn't fire inside other words; longer terms stay substring.
_SHORT = 3


def _tok_in(token, text):
    t = token.lower()
    if len(t) <= _SHORT:
        return re.search(rf"\b{re.escape(t)}\b", text) is not None
    return t in text


@lru_cache(maxsize=32)
def _title_re(pattern):
    return re.compile(pattern, re.I)


def is_technical_role(title, t):
    """Cheap positive title gate for track `t` (a config.UI_TRACKS entry)."""
    return bool(_title_re(t["tech_title_regex"]).search(title or ""))


@lru_cache(maxsize=32)
def _exclude_tables(track_id):
    """The [exclude.<track_id>] vocabulary, shaped for exclude_reason().
    Cached per track id — EXCLUDE_BY_TRACK is a load-time constant."""
    exc = getattr(config, "EXCLUDE_BY_TRACK", {}).get(track_id, {}) or {}
    return {
        "role_phrases": tuple(exc.get("role_phrases", [])),
        "title_tokens": tuple(exc.get("title_tokens", [])),
        "defense_strong": tuple(exc.get("defense_strong", [])),
        "defense_weak": tuple(exc.get("defense_weak", [])),
        "nonclinical": tuple(exc.get("nonclinical", [])),
    }


def exclude_reason(title, description="", allow_defense=False, *,
                   track_id):
    """Return a short reason string if the posting must be dropped, else
    None. Vocabulary comes from profile.toml [exclude.<track_id>].

    `allow_defense` skips the defense/military-radar exclusion ONLY —
    role-quality excludes (coordinator/scribe/data-entry) always apply. Set
    for WATCHED companies: watching a defense-adjacent employer means "I
    want its technical roles anyway"."""
    tables = _exclude_tables(track_id)
    title_l = (title or "").lower()
    text = f"{title} {description}".lower()

    # Word-boundary match so "scribe" doesn't fire on "describe", "data
    # entry" doesn't fire mid-word, etc.
    for phrase in tables["role_phrases"]:
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            return f"role: {phrase}"
    for tok in tables["title_tokens"]:
        if re.search(rf"\b{re.escape(tok)}\b", title_l):
            return f"role-title: {tok.upper()}"

    if not allow_defense and (tables["defense_strong"]
                              or tables["defense_weak"]):
        # EEO/benefits boilerplate scrub first: "military or veteran status"
        # must never read as a defense signal. STRONG terms are unambiguous
        # (one hit excludes); WEAK terms are words health postings use
        # innocently, so TWO DISTINCT weak hits are required.
        scrubbed = scrub_boilerplate(text)
        hit = next((d for d in tables["defense_strong"]
                    if _tok_in(d, scrubbed)), None)
        if hit:
            return f"defense: {hit}"
        weak = [d for d in tables["defense_weak"] if _tok_in(d, scrubbed)]
        if len(weak) >= 2:
            return f"defense: {'+'.join(weak[:3])}"
        # Military RF-radar: only exclude "radar" in a defense context.
        if "radar" in scrubbed and any(_tok_in(d, scrubbed) for d in
                                       ("military", "defense", "defence",
                                        "weapon", "warfare", "missile",
                                        "rf ")):
            return "defense: military radar"

    nc_hit = next((d for d in tables["nonclinical"]
                   if re.search(rf"\b{re.escape(d)}\b", text)), None)
    if nc_hit:
        return f"non-clinical: {nc_hit}"
    return None
