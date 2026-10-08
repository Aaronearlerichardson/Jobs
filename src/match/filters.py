"""Keyword relevance filtering, plus the term matcher every vocabulary gate
shares (`token_pattern` / `token_in`).

The keyword lists (CORE/DOMAIN/SKILL/INCLUDE/EXCLUDE_*) are bound from
config at import time as the SAME list objects config holds, and
src.crawl.runner.apply_keyword_focus mutates those lists in place, so a
track's focus is visible here without a reload (tests/test_tracks.py pins
that).

Relevance model (tiered):
  1. CORE match  -> standalone signal, relevant.
  2. DOMAIN + SKILL match -> adjacent medical/bio domain where your
     transferable skills apply. Relevant.
  3. `watch_titles=True` only -- a [policy] watch_division_titles TITLE.
     The division gate's escape hatch for a WATCHED conglomerate, whose
     aligned division is a plain engineering org. Off by default, so
     nothing outside that one gate can reach it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from typing import Final, Literal

from src import config
from src.config import (
    CORE_KEYWORDS,
    DOMAIN_KEYWORDS,
    EXCLUDE_PHRASES,
    EXCLUDE_TITLE_EXEMPT_PHRASES,
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
BOUNDED: Final = "bounded"
SUBSTRING = 0


def _bounded(term: str, short_len: int | Literal["bounded"]) -> bool:
    """Whether `term` gets \\b anchors under the `short_len` rule.

    A boundary needs a word character on the inside of it, so a term that
    starts or ends in punctuation can never be anchored -- `\\bc++\\b`
    matches nothing at all, including the literal "c++". src/match/gates.py
    anchored its exclude phrases unconditionally and so had exactly that
    hole: an exclude phrase ending in punctuation silently never fired.
    BOUNDED means "anchor where anchoring is meaningful", not "anchor".
    """
    if short_len == BOUNDED:
        return bool(term) and (term[0].isalnum() or term[0] == "_") \
            and (term[-1].isalnum() or term[-1] == "_")
    return term.isalpha() and len(term) <= short_len


def token_pattern(term: str, short_len: int | Literal["bounded"]) -> str:
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
def _term_rule(term: str, short_len: int | Literal["bounded"]
               ) -> tuple[str, re.Pattern[str] | None]:
    """(`term` lowercased, its boundary regex or None) under the
    token_pattern rule."""
    term = term.lower()
    return term, (re.compile(rf"\b{re.escape(term)}\b")
                  if _bounded(term, short_len) else None)


def token_in(term: str, text: str, short_len: int | Literal["bounded"]) -> bool:
    """Whether `term` occurs in `text` under the `token_pattern` rule.
    `text` must already be lowercase — every caller lowercases a posting
    once up front, and the long-term case is a plain substring test so a
    thousand keywords over a thousand postings stays cheap.

    >>> token_in("rome", "rome, italy", 4), token_in("rome", "chrome", 4)
    (True, False)
    >>> token_in("Boston", "bostonian", 4)
    True
    >>> token_in("weapon", "weapons", 3), token_in("cortical", "subcortical", 5)
    (True, True)

    BOUNDED anchors however long the term is; SUBSTRING never anchors:

    >>> token_in("scribe", "we describe things", BOUNDED)
    False
    >>> token_in("scribe", "we describe things", SUBSTRING)
    True

    Notes:
        The substring test runs first: a boundary match needs it too, and
        a C-level `in` rules most terms out before any regex runs. Until
        2026-09-24 every bounded term paid for a regex scan.
    """
    term, bounded = _term_rule(term, short_len)
    if term not in text:
        return False
    return bounded is None or bounded.search(text) is not None


@lru_cache(maxsize=256)
def _rules(terms: tuple[str, ...], short_len: int | Literal["bounded"]
           ) -> tuple[tuple[str, str, re.Pattern[str] | None], ...]:
    """(term, lowered, boundary regex or None) per term, in list order."""
    return tuple((t, *_term_rule(t, short_len)) for t in terms)


def first_hit(terms: Iterable[str], text: str,
              short_len: int | Literal["bounded"]) -> str | None:
    """The first of `terms`, in list order, that occurs in lowercase `text`
    under the `token_in` rule, or None.

    >>> first_hit(("scribe", "data entry"), "senior data entry clerk", BOUNDED)
    'data entry'
    >>> first_hit(("scribe",), "we describe things", BOUNDED) is None
    True
    >>> first_hit(["Entry", "data"], "data entry", BOUNDED)   # list order, as given
    'Entry'

    Notes:
        The per-term rules are cached per (terms, short_len) tuple, keyed
        on contents, so a list apply_keyword_focus mutated gets new rules.
    """
    for term, low, bounded in _rules(tuple(terms), short_len):
        if low in text and (bounded is None or bounded.search(text) is not None):
            return term
    return None


# --------------------------------------------------------------------- #
#  Relevance                                                             #
# --------------------------------------------------------------------- #

def _kw_in(text: str, keywords: Iterable[str]) -> bool:
    """A keyword hits `text` — acronyms on word boundaries (a bare "meg"
    would fire inside "omega" and flood aggregator sources with off-topic
    roles), longer terms as substrings (see SHORT_KEYWORD)."""
    return first_hit(keywords, text, SHORT_KEYWORD) is not None


def blank_phrases(text: str, phrases: Iterable[str | None]) -> str:
    """`text` with every one of `phrases` replaced by a space.

    The same move `scrub_boilerplate` makes on a posting body, over a list
    from the profile rather than a compiled regex: blank the wording that
    must not be read, then run the ordinary matcher over what is left.

    >>> blank_phrases("clinical data manager", ["data manager"])
    'clinical  '
    >>> blank_phrases("engineering manager", ["data manager"])
    'engineering manager'

    Matching is case-insensitive on the PHRASE side only -- every caller
    lowercases its text once up front, the same contract `token_in` keeps:

    >>> blank_phrases("scientific data manager", ["Data Manager"])
    'scientific  '

    Blank and whitespace-only phrases are ignored, so an empty chip left in
    the profile editor cannot blank the whole string:

    >>> blank_phrases("program manager", ["", "   ", None])
    'program manager'
    """
    for phrase in phrases:
        p = (phrase or "").strip().lower()
        if p:
            text = text.replace(p, " ")
    return text


def _excluded(title: str | None, text: str) -> bool:
    """EXCLUDE_PHRASES match anywhere; EXCLUDE_TITLE_PHRASES title-only,
    over a title EXCLUDE_TITLE_EXEMPT_PHRASES has been blanked out of.

    The profile-wide exclusion gate. Its per-track sibling is
    gates.exclude_reason, and the two are deliberately NOT one function --
    see the note at the top of gates.py for what differs and why. What
    they do share is `first_hit`, so neither can quietly grow a third
    spelling of "does this term occur in this text".

    SUBSTRING here: these phrases come from the profile as fragments meant
    to match anywhere, and `text` has already been through
    scrub_boilerplate. The per-track tables are single terms and get
    boundaries instead.

    Notes:
        The exemption pass (2026-09-18) exists because a title phrase is a
        WORD and a job title is not: "manager" in [exclude].title_phrases
        is meant to drop the people-managing titles (Program Manager,
        Engineering Manager), and it was also dropping every
        Clinical/Scientific/Research Data Manager -- an individual-
        contributor data role this profile's own candidate has held and
        asks for by name. Blanking the exempt phrase rather than skipping
        the gate keeps the OTHER title phrases live on the same title: a
        "Data Manager Intern" still loses to "intern".
    """
    if first_hit(EXCLUDE_PHRASES, text, SUBSTRING):
        return True
    title_l = blank_phrases((title or "").lower(), EXCLUDE_TITLE_EXEMPT_PHRASES)
    return bool(first_hit(EXCLUDE_TITLE_PHRASES, title_l, SUBSTRING))


@lru_cache(maxsize=4096)
def _scrub(frags: tuple[str, ...], text: str) -> str:
    """`text` with every match of the ORed `frags` blanked; no re.I."""
    return _boiler_re(frags).sub(" ", text)


@lru_cache(maxsize=8)
def _boiler_re(frags: tuple[str, ...]) -> re.Pattern[str]:
    """`frags` ORed into one case-sensitive regex."""
    return re.compile("|".join(frags))


def scrub_boilerplate(text: str | None) -> str:
    """Lowercase `text` with benefits/EEO/infra-health idioms blanked — for
    matchers whose keywords those idioms would otherwise false-trigger
    ("medical", "health", "drug", "military").

    `text` must already be lowercase, the `token_in` contract: the
    fragments are lowercase (the profile schema rejects an uppercase one)
    and match without re.I.

    >>> scrub_boilerplate("medical, dental, vision insurance; eeg data")
    ' ; eeg data'
    >>> scrub_boilerplate(None)
    ''

    Notes:
        Cached on (fragments, text), keyed on the fragment list's contents;
        dropping re.I took a call from 0.93 to 0.31 ms (2026-10-08).
    """
    # Boilerplate idioms that contain domain-looking words without meaning
    # them: benefits sections ("medical, dental, vision", "health savings
    # account", "drug-free workplace"), EEO statements ("military or
    # veteran status" — the defense gate's #1 false positive: 126 of 1116
    # stored JDs), vaccination policies, and infra-health prose ("service
    # health checks"). Scrubbed from text before keyword/exclusion
    # matching. Shared with the per-track defense gate (src/match/gates.py)
    # via this function. Source list: config.EXCLUDE_BOILERPLATE_PHRASES
    # (profile.toml [exclude] boilerplate_phrases); these are the fallback
    # when it is empty.
    default_boilerplate_phrases = (
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
    frags = tuple(getattr(config, "EXCLUDE_BOILERPLATE_PHRASES", None)
                  or default_boilerplate_phrases)
    return _scrub(frags, text or "")


def watch_division_title(title: str | None) -> str | None:
    """The config.WATCH_DIVISION_TITLES entry `title` carries, or None —
    profile [policy] watch_division_titles, matched BOUNDED against the
    title alone. See that constant for the scope argument; this is only the
    walk, and it goes through `first_hit` like every other vocabulary gate
    in this package.

    Read through the config PACKAGE, not a module-level import, so a test
    that narrows the list (monkeypatch.setattr(config, ...)) reaches it —
    the same rule the rest of config's callers keep.
    """
    return first_hit(getattr(config, "WATCH_DIVISION_TITLES", ()) or (),
                     (title or "").lower(), BOUNDED)


@lru_cache(maxsize=64)
def _untiered(include: tuple[str, ...], core: tuple[str, ...], domain: tuple[str, ...],
              skill: tuple[str, ...]) -> tuple[str, ...]:
    """The `include` keywords no tier lists, compared case-insensitively."""
    tiered = {k.lower() for k in core + domain + skill}
    return tuple(k for k in include if k.lower() not in tiered)


def is_relevant(title: str, description: str = "", *, watch_titles: bool = False) -> bool:
    """Whether a posting is in-field, under the tiered model at the top of
    this module.

    `watch_titles=True` adds tier 3 — the WATCHED-conglomerate title
    escape hatch. Only the two DIVISION gates pass it (src.crawl.triage's
    and src.ops.maintenance.keep_job's, which ask the same question of the
    same posting on the harvest and crawl paths), and only for a company
    that is watched; every other caller (the hydration ordering,
    the discovery filters) gets the unchanged two-tier answer.

    The [exclude] gate above it is NOT bypassed: a watched conglomerate's
    "Senior Manager, Software Engineering" still loses to the profile's
    "manager" title phrase, the same as anywhere else.
    """
    text = scrub_boilerplate((title + " " + description).lower())
    if _excluded(title, text):
        return False

    # Tier 1: core neurotech / specific job titles. Full-text scan.
    if _kw_in(text, CORE_KEYWORDS):
        return True

    # Legacy / dynamically-added keywords (not in any tier) act like Tier 1.
    # Keyed on the lists' CONTENTS: apply_keyword_focus mutates them.
    extras = _untiered(tuple(INCLUDE_KEYWORDS), tuple(CORE_KEYWORDS),
                       tuple(DOMAIN_KEYWORDS), tuple(SKILL_KEYWORDS))
    if extras and _kw_in(text, extras):
        return True

    # Tier 2 x Tier 3: adjacent medical/bio domain + transferable skill.
    # Head-only scan (1200 chars). Specific CORE terms (eeg, bci, neural
    # decoding) are signal wherever they appear, but generic domain words
    # deep in a posting are usually benefits boilerplate — "medical,
    # dental, vision" + "data" would tier-match nearly every US job ad if
    # the pairing scanned full text.
    head = scrub_boilerplate((title + " " + description[:1200]).lower())
    if _kw_in(head, DOMAIN_KEYWORDS) and _kw_in(head, SKILL_KEYWORDS):
        return True

    # Tier 3: the watched conglomerate's own engineering vocabulary.
    return bool(watch_titles and watch_division_title(title))
