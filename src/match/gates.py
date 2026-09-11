"""Config-driven posting gates.

The last two pieces of track behavior that used to live as code in the track
modules — the technical-title regex and the exclude tables — resolved
per-track from configuration instead:

  * `is_technical_role(title, t)` compiles `t["tech_title_regex"]`
    (profile.toml [tracks.*], engine defaults in src/config/tracks.py) — the cheap
    positive gate that keeps nurses/sales/admin titles away from any LLM
    scoring spend.
  * `exclude_reason(..., track_id=...)` reads the [exclude.<track_id>]
    tables (config.EXCLUDE_BY_TRACK) at call time — no hardcoded track key,
    so any user-defined track gets its own exclusion vocabulary. An absent
    or empty table makes the gate a no-op.

There are two exclusion gates in this package, and they stay two on
purpose. `filters._excluded` is the PROFILE-wide one: fragments from
[exclude] phrases/title_phrases, matched as plain substrings against text
`is_relevant` has already scrubbed, answering yes/no. `exclude_reason`
here is the PER-TRACK one: single terms from [exclude.<id>], matched on
word boundaries (so "scribe" does not fire inside "describe"), scrubbing
only for the defense stage, and answering WITH the term that did it —
because a posting dropped by a track needs to be debuggable in
triage_detail, while a posting that simply is not relevant does not.

Merging them would mean picking one of each pair of answers and silently
changing which postings survive. What they do share is the matcher:
every vocabulary walk in both goes through `filters.first_hit`.
"""

import re
from functools import lru_cache

from src import config

from src.match.filters import (BOUNDED, SHORT_EXCLUDE, first_hit,
                               scrub_boilerplate, token_in)

#: "radar" is only a defense signal in defense company. Not from the
#: profile: this is the shape of the false positive (radar appears in
#: automotive, weather and imaging postings), not a vocabulary choice.
_RADAR_CONTEXT = ("military", "defense", "defence", "weapon", "warfare",
                  "missile", "rf")


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

    # BOUNDED so "scribe" doesn't fire on "describe", "data entry" doesn't
    # fire mid-word. These used to be three hand-written `\b...\b` regexes
    # built per call; an exclude phrase that ended in punctuation could
    # therefore never match at all, since `\b` has nothing to anchor to.
    hit = first_hit(tables["role_phrases"], text, BOUNDED)
    if hit:
        return f"role: {hit}"
    hit = first_hit(tables["title_tokens"], title_l, BOUNDED)
    if hit:
        return f"role-title: {hit.upper()}"

    if not allow_defense and (tables["defense_strong"]
                              or tables["defense_weak"]):
        # EEO/benefits boilerplate scrub first: "military or veteran status"
        # must never read as a defense signal. STRONG terms are unambiguous
        # (one hit excludes); WEAK terms are words health postings use
        # innocently, so TWO DISTINCT weak hits are required. SHORT_EXCLUDE
        # rather than BOUNDED: these are plural-prone nouns, so "weapon"
        # must still reach "weapons".
        scrubbed = scrub_boilerplate(text)
        hit = first_hit(tables["defense_strong"], scrubbed, SHORT_EXCLUDE)
        if hit:
            return f"defense: {hit}"
        weak = [d for d in tables["defense_weak"]
                if token_in(d, scrubbed, SHORT_EXCLUDE)]
        if len(weak) >= 2:
            return f"defense: {'+'.join(weak[:3])}"
        # Military RF-radar: only exclude "radar" in a defense context
        # ("rf" is a bounded token, so "RF/microwave" counts and "perf"
        # does not).
        if "radar" in scrubbed and first_hit(_RADAR_CONTEXT, scrubbed,
                                             SHORT_EXCLUDE):
            return "defense: military radar"

    hit = first_hit(tables["nonclinical"], text, BOUNDED)
    if hit:
        return f"non-clinical: {hit}"
    return None
