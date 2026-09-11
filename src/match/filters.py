"""Keyword relevance filtering, plus the term matcher every vocabulary gate
shares (`token_pattern` / `token_in`).

The keyword lists (CORE/DOMAIN/SKILL/INCLUDE/EXCLUDE_*) are bound from
config at import time as the SAME list objects config holds, and
src.crawl.runner.apply_keyword_focus mutates those lists in place, so a
track's focus is visible here without a reload (tests/test_tracks.py pins
that). The boilerplate regex is compiled once at import.

Relevance model (tiered):
  1. CORE match  -> standalone signal, relevant.
  2. DOMAIN + SKILL match -> adjacent medical/bio domain where your
     transferable skills apply. Relevant.
"""

import re
from functools import lru_cache

from src import config
from src.config import (
    CORE_KEYWORDS,
    DOMAIN_KEYWORDS,
    EXCLUDE_PHRASES,
    EXCLUDE_TITLE_PHRASES,
    INCLUDE_KEYWORDS,
    SKILL_KEYWORDS,
)

# --------------------------------------------------------------------- #
#  Term matching                                                         #
# --------------------------------------------------------------------- #
#
# One rule for every vocabulary list in the crawler: a SHORT ALPHABETIC term
# matches on word boundaries, anything else as a plain substring. Short
# tokens are the false-positive engine ("ecog" in "recognized", "sf" in
# "surf", "us" in "campus"); longer terms are distinctive enough that
# substring matching is what covers their inflections ("weapon" ->
# "weapons", "cortical" <- "subcortical"). Non-alphabetic terms never get
# boundaries: `\b` needs a word character on the inside, so "c++" or
# "u.s." would silently stop matching at all.
#
# Where the short/long line falls depends on the vocabulary, so each caller
# has its own threshold, all kept here so the reasoning sits in one place:

# Relevance keywords: acronyms run to five letters (eeg, ecog, ieeg, fnirs,
# fmri); six-letter words are real words whose inflections we want.
SHORT_KEYWORD = 5
# Per-track exclusion vocabulary (src/match/gates.py): the only ambiguous tokens
# are the two/three-letter ones (sdr, bdr, rf); the defense/role terms are
# plural-prone nouns ("drone", "weapon", "army") that must stay substring
# from four letters up so "drones" and "weapons" still hit.
SHORT_EXCLUDE = 3
# Remote-work tokens and region codes (src/match/locality.py): us / uk /
# eu / wfh need boundaries; "asia", "emea", "america" stay substring so the
# longer forms ("americas", "southeast asia") match too.
SHORT_REMOTE = 3
# Place names pulled out of page text (src/match/locality.py): four-letter towns
# and two-letter state codes hide inside ordinary words ("rome" in "chrome",
# "nc" in "clinic"); five letters and up are distinctive.
SHORT_PLACE = 4


#: `short_len` values for the two vocabularies that do not want the
#: length rule at all. BOUNDED asks for word boundaries on every term
#: however long -- the per-track exclude tables, where "scribe" must not
#: fire inside "describe" and "data entry" must not fire mid-word.
#: SUBSTRING asks for none -- the profile's global EXCLUDE_PHRASES, which
#: are written as fragments meant to match anywhere.
BOUNDED = "bounded"
SUBSTRING = 0


def _bounded(term, short_len):
    """Whether `term` gets \\b anchors under the `short_len` rule.

    A boundary needs a word character on the inside of it, so a term that
    starts or ends in punctuation can never be anchored -- `\\bc++\\b`
    matches nothing at all, including the literal "c++". src/match/gates.py
    anchored its exclude phrases unconditionally and so had exactly that
    hole: an exclude phrase ending in punctuation silently never fired.
    BOUNDED means "anchor where anchoring is meaningful", not "anchor".
    """
    if short_len is BOUNDED:
        return bool(term) and (term[0].isalnum() or term[0] == "_") \
            and (term[-1].isalnum() or term[-1] == "_")
    return term.isalpha() and len(term) <= short_len


def token_pattern(term, short_len):
    """Regex source that matches `term` literally: on word boundaries when it
    is an alphabetic token of at most `short_len` characters, as a bare
    substring otherwise. For building alternations; compile with re.I.

    >>> token_pattern("rome", 4)
    '\\\\brome\\\\b'
    >>> token_pattern("boston", 4)
    'boston'
    >>> token_pattern("c++", 4)           # no boundary can follow "+"
    'c\\\\+\\\\+'
    >>> token_pattern("san jose", 4)
    'san\\\\ jose'
    """
    esc = re.escape(term)
    return rf"\b{esc}\b" if _bounded(term, short_len) else esc


@lru_cache(maxsize=4096)
def _short_re(term):
    return re.compile(rf"\b{re.escape(term)}\b")


def token_in(term, text, short_len):
    """Whether `term` occurs in `text` under the `token_pattern` rule.
    `text` must already be lowercase — every caller lowercases a posting
    once up front, and the long-term case is a plain substring test so a
    thousand keywords over a thousand postings stays cheap.

    >>> token_in("rome", "rome, italy", 4), token_in("rome", "chrome", 4)
    (True, False)
    >>> token_in("Boston", "bostonian", 4)
    True

    BOUNDED anchors however long the term is; SUBSTRING never anchors:

    >>> token_in("scribe", "we describe things", BOUNDED)
    False
    >>> token_in("scribe", "we describe things", SUBSTRING)
    True
    """
    term = term.lower()
    if _bounded(term, short_len):
        return _short_re(term).search(text) is not None
    return term in text


def first_hit(terms, text, short_len):
    """The first of `terms` that occurs in `text`, or None.

    The shape every vocabulary gate in this package was writing out for
    itself -- walk a list, return what matched so the verdict can say why.
    Five loops over four vocabularies, in two modules, with three
    different spellings of the match test between them.

    >>> first_hit(("scribe", "data entry"), "senior data entry clerk", BOUNDED)
    'data entry'
    >>> first_hit(("scribe",), "we describe things", BOUNDED) is None
    True
    """
    return next((t for t in terms if token_in(t, text, short_len)), None)


# --------------------------------------------------------------------- #
#  Relevance                                                             #
# --------------------------------------------------------------------- #

def _kw_in(text, keywords):
    """Any keyword hits `text` — acronyms on word boundaries (a bare "meg"
    would fire inside "omega" and flood aggregator sources with off-topic
    roles), longer terms as substrings (see SHORT_KEYWORD)."""
    return any(token_in(k, text, SHORT_KEYWORD) for k in keywords)


def _excluded(title, text):
    """EXCLUDE_PHRASES match anywhere; EXCLUDE_TITLE_PHRASES title-only.

    The profile-wide exclusion gate. Its per-track sibling is
    gates.exclude_reason, and the two are deliberately NOT one function --
    see the note at the top of gates.py for what differs and why. What
    they do share is `first_hit`, so neither can quietly grow a third
    spelling of "does this term occur in this text".

    SUBSTRING here: these phrases come from the profile as fragments meant
    to match anywhere, and `text` has already been through
    scrub_boilerplate. The per-track tables are single terms and get
    boundaries instead.
    """
    if first_hit(EXCLUDE_PHRASES, text, SUBSTRING):
        return True
    return bool(first_hit(EXCLUDE_TITLE_PHRASES, (title or "").lower(),
                          SUBSTRING))


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
# text before keyword/exclusion matching. Shared with the per-track defense
# gate (src/match/gates.py) via scrub_boilerplate(). Source list:
# config.EXCLUDE_BOILERPLATE_PHRASES (profile.toml [exclude]
# boilerplate_phrases); these are the fallback when it is empty.
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
