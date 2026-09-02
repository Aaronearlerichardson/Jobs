"""Keyword + location filtering.

Reads CORE/DOMAIN/SKILL_KEYWORDS, EXCLUDE_PHRASES, and LOCATION_* from
config at call time, so `--expand-live` and friends can mutate those
lists and this module will see the updated values on the next call.

Relevance model (tiered):
  1. CORE match  -> standalone signal, relevant.
  2. DOMAIN + SKILL match -> adjacent medical/bio domain where your
     transferable skills apply. Relevant.
"""

import re

import config
from config import (
    CORE_KEYWORDS,
    DOMAIN_KEYWORDS,
    EXCLUDE_PHRASES,
    EXCLUDE_TITLE_PHRASES,
    INCLUDE_KEYWORDS,
    SKILL_KEYWORDS,
)


# --------------------------------------------------------------------- #
#  Relevance                                                             #
# --------------------------------------------------------------------- #

def _kw_match(kw, text):
    """
    Case-insensitive keyword hit. Short single-token alphabetic keywords
    (acronyms: eeg, bci, ecog, ieeg, meg, mri, dsp, ...) use word-boundary
    matching — plain substring fires inside ordinary words ("ecog" in
    "recognized", "meg" in "omega") and floods aggregator sources with
    off-topic roles. Multi-word phrases and longer tokens stay substring
    so "subcortical" still matches "cortical".
    """
    k = kw.lower()
    if k.isalpha() and len(k) <= 5:
        return re.search(rf"\b{re.escape(k)}\b", text) is not None
    return k in text


def _kw_in(text, keywords):
    return any(_kw_match(k, text) for k in keywords)


def _excluded(title, text):
    """EXCLUDE_PHRASES match anywhere; EXCLUDE_TITLE_PHRASES title-only."""
    if any(p.lower() in text for p in EXCLUDE_PHRASES):
        return True
    t = (title or "").lower()
    return any(p.lower() in t for p in EXCLUDE_TITLE_PHRASES)


# DOMAIN+SKILL pairing only reads the posting head. Specific CORE terms
# (eeg, bci, neural decoding) are signal wherever they appear, but generic
# domain words deep in a posting are usually benefits boilerplate —
# "medical, dental, vision" + "data" would tier-match nearly every US job
# ad if the pairing scanned full text.
_PAIR_SCAN_CHARS = 1200

# Boilerplate idioms that contain domain-looking words without meaning them:
# benefits sections ("medical, dental, vision", "health savings account",
# "drug-free workplace"), EEO statements ("military or veteran status" — the
# defense gate's #1 false positive: 126 of 1116 stored JDs), vaccination
# policies, and infra-health prose ("service health checks"). Scrubbed from
# text before keyword/exclusion matching. Shared with the local track's
# defense gate via scrub_boilerplate(). Source list: config.EXCLUDE_BOILERPLATE_PHRASES
# (profile.toml [exclude] boilerplate_phrases).
_DEFAULT_BOILERPLATE_PHRASES = (
    # benefits
    r"medical[,/&\s]+(?:dental|vision)(?:[,/&\s]+(?:dental|vision))?(?:\s+(?:insurance|coverage|benefits|plans?))?",
    r"health\s+(?:insurance|savings|benefits?|plans?|coverage|reimbursement)",
    r"health\s*(?:&|and)\s*well(?:ness|-?being)",
    r"drug[-\s]free\s+work(?:place|\s*environment)",
    r"drug\s+(?:screen(?:ing)?|test(?:ing)?)",
    r"(?:covid(?:-19)?\s+)?vaccin(?:e|ation)\s+(?:policy|requirement|status)",
    # EEO
    r"military\s+(?:or\s+|and\s+|/\s*)?veteran'?s?\s+status",
    r"veteran'?s?\s+(?:or\s+|and\s+|/\s*)?military\s+status",
    r"military\s+(?:status|service|spouses?|caregivers?|leave|families|obligations?)",
    r"protected\s+veterans?", r"veterans?'?s?\s+status", r"uniformed\s+services?",
    r"status\s+as\s+an?\s+(?:protected\s+)?veteran",
    # infra-health prose
    r"(?:system|service|cluster|application|platform|code(?:base)?)\s+health",
    r"health\s+(?:checks?|monitoring|metrics)",
    r"health\s+of\s+(?:the|our|your)",
)
_BOILERPLATE_RE = re.compile(
    "|".join(getattr(config, "EXCLUDE_BOILERPLATE_PHRASES", None) or _DEFAULT_BOILERPLATE_PHRASES),
    re.I,
)


def scrub_boilerplate(text):
    """`text` with benefits/EEO/infra-health idioms blanked — for matchers
    whose keywords those idioms would otherwise false-trigger ("medical",
    "health", "drug", "military")."""
    return _BOILERPLATE_RE.sub(" ", text or "")


def is_relevant(title, description=""):
    text = scrub_boilerplate((title + " " + description).lower())
    if _excluded(title, text):
        return False

    # Tier 1: core neurotech / specific job titles. Full-text scan.
    if _kw_in(text, CORE_KEYWORDS):
        return True

    # Legacy / dynamically-added keywords (not in any tier) act like Tier 1.
    tiered = {k.lower() for k in CORE_KEYWORDS + DOMAIN_KEYWORDS + SKILL_KEYWORDS}
    extras = [k for k in INCLUDE_KEYWORDS if k.lower() not in tiered]
    if extras and _kw_in(text, extras):
        return True

    # Tier 2 x Tier 3: adjacent medical/bio domain + transferable skill.
    # Head-only scan — see _PAIR_SCAN_CHARS.
    head = scrub_boilerplate((title + " " + description[:_PAIR_SCAN_CHARS]).lower())
    return _kw_in(head, DOMAIN_KEYWORDS) and _kw_in(head, SKILL_KEYWORDS)
