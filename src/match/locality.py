"""
Single source of truth for WHERE a posting is: in your area, remote, or
neither.

Three questions, one vocabulary:

  * is this posting local (NC_RE / NC_HQ_RE / is_nc)
  * what location does this page text even name (location_snippet)
  * is this posting remote, and remote for someone in the US
    (remote_signal / remote_signal_for / us_eligible)

Every term comes from profile.toml -- [locality] for the first two,
[locations] for the third, with built-in fallbacks when a section is
unconfigured -- so none of it is hard-coded to one region. NC_RE /
NC_HQ_RE / is_nc are the historical public names, kept for import
stability; they have been region-agnostic for a while.

src/ats/board/company, discovery/local_sourcing, the runner, the
harvest triage pass and the webapp all delegate here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Protocol, cast

from src import config
from src.match.filters import (SHORT_PLACE, SHORT_REMOTE, first_hit,
                               token_pattern)
from src.rows import FetchedJob

# Word-boundary for short/ambiguous tokens (so "nc" doesn't hit "clinic",
# "sf" doesn't hit "surf"); substring for distinctive multi-char names.
_WB = [t for t in config.LOCALITY_WORD_TOKENS if t]
_SUB = [t for t in config.LOCALITY_SUBSTRINGS if t]

# A configured place token, anywhere in the text. Private: a stored
# LOCATION is judged by NC_RE / is_nc below, which read it one segment at a
# time; only geo_mode reads free body text with this raw form.
_NC_TOKEN_RE = re.compile(
    "|".join([rf"\b{re.escape(t)}\b" for t in _WB]
             + [re.escape(t) for t in _SUB])
    or r"(?!x)x",   # match-nothing when no locality terms are configured
    re.I,
)

# Stricter "<place>, ST" address form — a company-HQ/office signal that holds
# even when a company has zero current openings. Built from every place term
# followed (within a few chars) by a configured state suffix.
_SUFFIX = [re.escape(s) for s in config.LOCALITY_STATE_SUFFIX if s]
NC_HQ_RE = re.compile(
    (rf"\b(?:{'|'.join(re.escape(t) for t in _WB + _SUB)})\b"
     rf"[\s,.\-]{{0,4}}(?:{'|'.join(_SUFFIX)})\b"
     if (_WB or _SUB) and _SUFFIX else r"(?!x)x"),
    re.I,
)


# --------------------------------------------------------------------------- #
#  Multi-site locations: one segment, one verdict                              #
# --------------------------------------------------------------------------- #
#
# A multi-state/multi-country ATS packs several offices into one location
# STRING ("US - CA - San Diego", "UK - County Durham - Barnard Castle",
# "US, Blue Bell (ICON); Canada, Burlington"). A bare NC_RE.search over the
# whole string is fooled whenever a configured local city name collides
# with a place somewhere else: "Cary" is also a town in Illinois,
# "Burlington" in Wisconsin/Vermont/Ontario, "Durham" a county in England.
# is_nc below judges the string one SEGMENT at a time (split on ";" and
# "|", the separators multi-site rows use between whole offices) and,
# within a segment, discounts a bare city-token hit when that SAME segment
# also names a different US state or a non-US country and no configured
# state_suffix -- so "Durham, NC; Boston, MA" stays local (the NC segment
# names NC) while "Cary, Illinois" and "UK - County Durham - Barnard
# Castle" do not (their one segment names somewhere else).
#
# Every US state but this profile's own, name and postal code (a segment
# naming OUR state is not "a different state"). Non-US countries/regions
# reuse _NON_US_REGIONS/non_us_place below (the remote-eligibility
# vocabulary already covers "uk", "canada", "europe", ...) rather than a
# second copy.
_ALL_US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct",
    "delaware": "de", "florida": "fl", "georgia": "ga", "hawaii": "hi",
    "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia",
    "kansas": "ks", "kentucky": "ky", "louisiana": "la", "maine": "me",
    "maryland": "md", "massachusetts": "ma", "michigan": "mi",
    "minnesota": "mn", "mississippi": "ms", "missouri": "mo",
    "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
    "new york": "ny", "north carolina": "nc", "north dakota": "nd",
    "ohio": "oh", "oklahoma": "ok", "oregon": "or", "pennsylvania": "pa",
    "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
    "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
}
_OWN_STATE_TERMS = {s.strip().lower() for s in config.LOCALITY_STATE_SUFFIX if s}
_OTHER_STATES = {name: abbr for name, abbr in _ALL_US_STATES.items()
                 if name not in _OWN_STATE_TERMS and abbr not in _OWN_STATE_TERMS}

_OTHER_STATE_NAME_RE = re.compile(
    "|".join(rf"\b{re.escape(n)}\b" for n in _OTHER_STATES) or r"(?!x)x", re.I)

# A 2-letter state code only counts when it sits where an ATS puts one:
# right after a comma/dash separator and followed by the segment's end or
# another separator -- "US - CA - San Diego", "Rockford, IL" -- never a
# bare substring search, which would hit "Health IT" or "the OR suite".
# Case-sensitive on purpose: an ATS's own code is written in caps, and a
# lowercase "or"/"in"/"hi" is almost always the ordinary word.
_OTHER_STATE_ABBR_RE = re.compile(
    rf"[,\-]\s*({'|'.join(sorted({a.upper() for a in _OTHER_STATES.values()}))})"
    rf"(?=\s*(?:[,\-/]|$))"
    if _OTHER_STATES else r"(?!x)x")

# The configured state suffix itself ("nc" / "north carolina"), independent
# of any city: naming the state directly is unambiguous, so it must win
# outright even when another state sits right next to it in the same
# segment with no ";"/"|" between them -- "Virginia/North Carolina" (a
# dual-territory sales-rep posting) and "Washington County, NC" both name
# OUR state directly and must not be discounted for also naming Virginia
# or containing the word "Washington".
_OWN_STATE_RE = re.compile(rf"\b(?:{'|'.join(_SUFFIX)})\b" if _SUFFIX else r"(?!x)x", re.I)

# _NC_TOKEN_RE's terms lowercased, for _names_place's case-sensitive scan.
_SUB_LOW = tuple(t.lower() for t in _SUB)
_WB_LOW_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t.lower()) for t in _WB) + r")\b"
    if _WB else r"(?!x)x")
_ASCII_TERMS = all(t.isascii() for t in _WB + _SUB)


def _names_place(text: str) -> bool:
    """bool(_NC_TOKEN_RE.search(text)): whether any configured place term
    occurs in `text`, case-insensitively, word tokens on boundaries.
    tests/test_locality.py pins the agreement, fold characters included.

    Notes:
        With ASCII terms and none of the fold characters in
        `text`, a re.I match is exactly a case-sensitive match on
        text.lower(): lower() keeps every other character's length and
        word-ness, and no other character folds onto ASCII (checked over
        every code point, 2026-09-24). The lowered scan is a plain `in` per
        substring term plus a small boundary regex, 5-8x faster than the
        re.I alternation; geo_mode runs it on every harvested row.
    """
    # The fold characters: the only non-ASCII characters re.I folds onto an
    # ASCII character (dotted and dotless i, long s, Kelvin sign). U+0130 is
    # also the one character whose lower() is longer than itself.
    if not _ASCII_TERMS or re.search("[\u0130\u0131\u017f\u212a]", text):
        return _NC_TOKEN_RE.search(text) is not None
    low = text.lower()
    return (any(t in low for t in _SUB_LOW)
            or _WB_LOW_RE.search(low) is not None)


def _segment_is_local(segment: str) -> bool:
    """Whether one ";"/"|"-separated location SEGMENT counts as local (see
    is_nc). Naming the configured state directly (NC_HQ_RE's place+suffix
    pairing, or the bare suffix itself) always wins outright; short of
    that, a bare city-token hit is discounted when the segment also names
    a different US state or a non-US country/region."""
    if not _NC_TOKEN_RE.search(segment):
        return False
    if NC_HQ_RE.search(segment) or _OWN_STATE_RE.search(segment):
        return True
    if _OTHER_STATE_NAME_RE.search(segment) or _OTHER_STATE_ABBR_RE.search(segment):
        return False
    if non_us_place(segment):
        return False
    return True


class LocationRE(Protocol):
    """What judges a LOCATION string local: a compiled regex, or NC_RE."""

    def search(self, text: str, /) -> re.Match[str] | None: ...


class _LocalLocationRE:
    """NC_RE: the configured-locality test for a LOCATION string, shaped
    like a compiled regex so it drops in wherever one is expected (every
    fetcher's `loc_re`, store.ranked_jobs' `location_re`, the digest and
    web UI geo buckets).

    `search(text)` returns the first place-token match inside the first
    segment _segment_is_local accepts, or None; tests/test_locality.py::
    TestSegments pins both with strings built from the active profile.

    >>> NC_RE.search("") is None, NC_RE.search(None) is None
    (True, True)
    """

    def search(self, text: str | None) -> re.Match[str] | None:
        text = text or ""
        pos = 0
        for seg in re.split(r"[;|]", text):
            if _segment_is_local(seg):
                return _NC_TOKEN_RE.search(text, pos, pos + len(seg))
            pos += len(seg) + 1         # every separator is one character
        return None


NC_RE = _LocalLocationRE()


def is_nc(text: str | None) -> bool:
    """True if `text` names a configured-local location (profile [locality]).

    Judged per ";"/"|" segment (see _segment_is_local): one local segment
    keeps a multi-site row local; a configured city in a segment that also
    names another US state or a non-US country is not local unless that
    segment names the configured state too; a bare city with no state or
    country named stays local. tests/test_locality.py::TestSegments pins
    each rule with strings built from the active profile.

    >>> is_nc(""), is_nc(None)
    (False, False)

    Notes:
        Live store, 2026-09-17: over 30,823 distinct stored locations the
        per-segment rule flipped 103 (343 rows) from local to not local
        and none the other way; every flip was a same-named place in
        another state or country (a Cary in Illinois, a Durham county in
        England, Burlingtons in five states and Ontario). One shape stays
        a false positive: a same-named city with no state at all ("<Name>
        Medical Center <City> - 252 <Street> St"). A local posting spells
        out a street address the same way, so no rule here separates them.
    """
    return NC_RE.search(text) is not None


# --------------------------------------------------------------------------- #
#  Pulling a location OUT of scraped page text                                 #
# --------------------------------------------------------------------------- #
#
# Custom/legacy boards (SuccessFactors tables, PeopleAdmin feeds, hand-rolled
# careers pages) publish no location field — the city is just words in the
# row. Scrapers recover it by searching that text for a place they recognise,
# which means the vocabulary has to come from [locality]; a hard-coded city
# list only ever works for the person who wrote it. Here the word/substring
# split is by length (filters.SHORT_PLACE), not the profile's own
# word_tokens/substrings classification: state codes and four-letter towns
# get boundaries whichever list they came from.

_SNIPPET_ALTS = [token_pattern(t, SHORT_PLACE)
                 for t in (_WB + _SUB + [s for s in config.LOCALITY_STATE_SUFFIX if s])]
# "Remote" always counts: it's a location on every board, in every field.
_SNIPPET_ALTS.append(r"\bremote\b")

# Shared with the field grammar's cut_date_tail transform
# (src/ats/board/fields.py) so the vocabulary lives in one place.
MONTH_ABBRS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
              "Sep", "Sept", "Oct", "Nov", "Dec")

# A recognised place plus a bounded, address-shaped tail: more comma/dash-
# separated Capitalized words or digit runs, so "Durham" in a table cell
# still comes back as "Durham, NC 27701" or "Remote, United States". The
# tail is deliberately case-SENSITIVE (scoped off with (?-i:...), the rest
# of the pattern stays case-insensitive) and blind to a month name, so it
# stops cold at a glued-on posting date instead of swallowing it, and it
# stops at the first lowercase word, which is where prose actually starts
# -- a bare 0-40-char capture used to walk into the next sentence,
# "North Carolina at Chapel Hill is seeking two tenure-tr[ack ...]" (a
# PeopleAdmin body, 2026-09-17); this stops at "at".
LOCATION_SNIPPET_RE = re.compile(
    rf"(?:{'|'.join(_SNIPPET_ALTS)})"
    rf"(?-i:(?:[\s,.\-]{{1,3}}(?!(?:{'|'.join(MONTH_ABBRS)})\b)"
    rf"(?:[A-Z][a-zA-Z]{{0,15}}|\d{{3,10}}))*)",
    re.I)


# What location_snippet answers when it finds no place; location_unknown
# reads it back as no place at all.
_NO_PLACE = "See posting"


def location_snippet(text: str | None, default: str = _NO_PLACE) -> str:
    """The first location-looking phrase in `text`, or `default`: a
    configured place (or "remote") plus the Capitalized words and digit
    runs that follow it, stopping at a month name or the first lowercase
    word. tests/test_locality.py::TestSnippet pins the configured-place
    cases.

    >>> location_snippet("Remote, United States Sep 9, 2026 Remote, Unit")
    'Remote, United States'
    >>> location_snippet("remote roles are open")
    'remote'
    >>> location_snippet("no place named here")
    'See posting'
    """
    m = LOCATION_SNIPPET_RE.search(text or "")
    return m.group(0).strip(" ,-") if m else default


def location_unknown(location: str | None) -> bool:
    """True when `location` names no place at all: blank, location_snippet's
    own placeholder ("See posting", any case), a literal "Unknown", or a Workday board's
    "<N> Location(s)" listing text -- three spellings of the same fact, that
    the geo gate has nothing to read yet and a detail call is what would
    fix it (see src.crawl.triage's module docstring and
    board.company.needs_detail/hydrate_description).

    >>> location_unknown(None), location_unknown("  ")
    (True, True)
    >>> location_unknown("See posting"), location_unknown("SEE POSTING")
    (True, True)
    >>> location_unknown("Unknown"), location_unknown(" unknown ")
    (True, True)
    >>> location_unknown("2 Locations"), location_unknown("1 Location")
    (True, True)
    >>> location_unknown("Durham, NC")
    False
    """
    loc = (location or "").strip().lower()
    # Workday's "<N> Locations" listing text for a multi-site req; the real
    # list comes with the detail JSON (board.company.hydrate_description).
    return (loc in ("", "unknown", _NO_PLACE.lower())
            or bool(re.match(r"^\s*\d+\s+locations?\s*$", loc, re.I)))


# --------------------------------------------------------------------------- #
#  Remote eligibility                                                          #
# --------------------------------------------------------------------------- #
#
# Was src/match/remote_filter.py. geo_mode() below asked it one
# question and imported it INSIDE the function body to do so,
# which is the shape a module split leaves behind when the two
# halves were never really separable: locality answers "where is
# this job", and "remote" is one of the answers.
#
# Side-effect-free, like the rest of this module. The sweep runner
# (src/crawl/runner.py) stamps `remote_signal` on its rows, and the
# webapp reads it. None of it touches the locality gate above, so the
# onsite crawl path is undisturbed by changes here.
#
# Two precision rules keep false positives down -- these get
# surfaced for human review before anyone emails anyone:
#
#   * In the body, "distributed" and "anywhere" only count in a
#     WORKFORCE context ("distributed team", "work from
#     anywhere"). A bare "distributed" in a body almost always
#     means distributed systems or distributed training, which is
#     ubiquitous in ML roles and says nothing about where you sit.
#   * In the location field -- short and ATS-curated -- a bare
#     "remote" / "distributed" / "anywhere" token is trusted.

# Location-field signals. The location string is short and ATS-curated
# ("Remote", "Remote, US", "Remote - United States", "Distributed"), so a
# bare token here is a reliable signal. Source: config.REMOTE_LOC_TOKENS
# (profile.toml [locations] remote_tokens); falls back to these defaults.
_DEFAULT_LOC_REMOTE_TOKENS = (
    "remote",
    "work from home",
    "work-from-home",
    "work from anywhere",
    "work-from-anywhere",
    "anywhere",
    "distributed",
    "telecommute",
    "home-based",
    "home based",
    "virtual",
    "wfh",
)
_LOC_REMOTE_TOKENS = tuple(getattr(config, "REMOTE_LOC_TOKENS", None) or _DEFAULT_LOC_REMOTE_TOKENS)

# Body signals. Stricter than the location field: "distributed" / "anywhere"
# must appear in a workforce phrase, never bare, to avoid matching
# "distributed systems", "distributed training", "anywhere from 5-10 years".
# Source: config.REMOTE_BODY_PHRASES (profile.toml [locations] remote_phrases).
_DEFAULT_BODY_REMOTE_PHRASES = (
    "fully remote",
    "100% remote",
    "remote-first",
    "remote first",
    "remote-friendly",
    "remote friendly",
    "remote position",
    "remote role",
    "remote opportunity",
    "remote (us",
    "remote - us",
    "remote, us",
    "us-remote",
    "us remote",
    "remote within the us",
    "remote in the us",
    "work from home",
    "work-from-home",
    "work from anywhere",
    "work-from-anywhere",
    "from anywhere",
    "anywhere in the world",
    "anywhere in the u",          # "...the US" / "...the United States"
    "home-based",
    "home based",
    "telecommute",
    "distributed team",
    "distributed company",
    "distributed workforce",
    "fully distributed",
    "globally distributed",
    "remote/distributed",
    "remote or distributed",
    "this is a remote",
    "this role is remote",
    "position is remote",
)
_BODY_REMOTE_PHRASES = tuple(getattr(config, "REMOTE_BODY_PHRASES", None) or _DEFAULT_BODY_REMOTE_PHRASES)

# Hard negations. If any of these appear, the posting is treated as NOT
# remote-eligible regardless of stray "remote" mentions. Conservative by
# design — the track prefers to drop a borderline posting over emailing a
# false positive. Source: config.REMOTE_HARD_NEGATIONS (profile.toml
# [locations] hard_negations).
_DEFAULT_HARD_NEGATIONS = (
    "not remote",
    "no remote",
    "non-remote",
    "not a remote",
    "not eligible for remote",
    "not available for remote",
    "no remote option",
    "remote is not",
    "remote work is not",
    "this role is not remote",
    "this position is not remote",
    "on-site only",
    "onsite only",
    "on site only",
    "fully on-site",
    "fully onsite",
    "in-office only",
    "in office only",
    "must be on-site",
    "must be onsite",
    "must be in office",
    "must be in-office",
    "must be located in",
    "relocation required",
    "relocation is required",
    "no relocation",
)
_HARD_NEGATIONS = tuple(getattr(config, "REMOTE_HARD_NEGATIONS", None) or _DEFAULT_HARD_NEGATIONS)


def remote_signal(location: str | None, description: str | None = "") -> str | None:
    """Return the phrase that marks this posting remote-eligible, or None.

    Returned phrase is handy for the precision sanity-check sample output.

    >>> remote_signal("United States"), remote_signal("USA"), remote_signal("US")
    ('location:nationwide', 'location:nationwide', 'location:nationwide')
    >>> remote_signal("United States of America", "on-site only")
    >>> remote_signal("Durham, NC, United States") is None
    True
    """
    loc = (location or "").lower()
    body = (description or "").lower()

    # A hard negation anywhere vetoes the posting.
    if first_hit(_HARD_NEGATIONS, loc + " \n " + body, SHORT_REMOTE):
        return None

    hit = first_hit(_LOC_REMOTE_TOKENS, loc, SHORT_REMOTE)
    if hit:
        return f"location:{hit}"
    # A field that is only the country: a nationwide posting, which an ATS
    # files that way when no office is named (Lyra Health, Pedestal Health).
    if re.fullmatch(r"\s*(?:united states(?: of america)?|u\.?s\.?a?\.?)\s*", loc):
        return "location:nationwide"

    hit = first_hit(_BODY_REMOTE_PHRASES, body, SHORT_REMOTE)
    if hit:
        return f"body:{hit}"

    return None


def remote_signal_for(job: FetchedJob) -> str | None:
    """Job-dict-aware remote signal.

    Prefers a structured hint stamped by the fetcher (JSON-LD
    jobLocationType=TELECOMMUTE, Lever workplaceType, Ashby isRemote,
    remote-only boards) over phrase matching — the ATS knows better than
    a regex. Falls back to remote_signal() on the location/body text.
    """
    hint = job.get("remote_hint")
    if hint:
        return f"hint:{hint}"
    return remote_signal(job.get("location", ""), job.get("description", ""))


# ─── US eligibility ──────────────────────────────────────────────────────
#
# "Remote" is not "remote for you": boards are full of "Philippines
# Remote" / "Remote - EMEA" roles a US applicant can't take. Checked
# against the LOCATION field only — it's short and curated, while body
# text mentions regions for all kinds of reasons ("customers in Europe").
# Unknown/ambiguous locations pass: better a stray non-US posting in the
# digest than a real US-remote role silently dropped.

def extend_vocab(builtin: tuple[str, ...], extra: Iterable[str] | None) -> tuple[str, ...]:
    """`builtin` followed by what `extra` adds, lowercased and without repeats.

    >>> extend_vocab(("us", "usa"), ["USA", " America ", "", "uk"])
    ('us', 'usa', 'america', 'uk')
    >>> extend_vocab(("us",), None), extend_vocab(("us",), [])
    (('us',), ('us',))
    """
    return tuple(dict.fromkeys((*builtin, *(t.strip().lower() for t in extra or () if t.strip()))))


_DEFAULT_US_MARKERS = (
    "us", "u.s", "usa", "united states", "america", "americas",
    "north america", "worldwide", "global", "anywhere", "world",
)
_US_MARKERS = extend_vocab(_DEFAULT_US_MARKERS, getattr(config, "REMOTE_US_MARKERS", None))

_NON_US_REGIONS = extend_vocab(tuple(config.NON_US_PLACES["regions"]),
                               getattr(config, "REMOTE_NON_US_REGIONS", None))
# non_us_place's patterns, fixed at import. A name matches on word
# boundaries, so "india" misses "Indian Trail". A code, in capitals only,
# matches in address position: after a comma or whitespace, then a
# separator, ")", a Canadian postal code, an AU postcode or the end -- so
# "Raleigh, ON-SITE" and "Hands on" are not hits. The segment names
# ("ontario", "new brunswick") stay out of _NON_US_REGIONS: is_nc has ruled
# out a same-segment US state by now, while us_eligible would misread
# Ontario CA and New Brunswick NJ.
_NON_US_NAME_RE = re.compile(
    "|".join(rf"\b{re.escape(t)}\b" for t in (*_NON_US_REGIONS, *config.NON_US_PLACES["segment_names"])),
    re.I)
_NON_US_CODE_RE = re.compile(
    rf"(?:,\s*|\s)(?:{'|'.join(map(re.escape, config.NON_US_PLACES['codes']))})"
    r"(?=\s*[,/)]|\s+-\s|\s+[A-Z]\d[A-Z]\b|\s+\d{4}\b|\s*$)")


def non_us_place(segment: str) -> bool:
    """Whether `segment` names a non-US country, province or state.

    >>> [non_us_place(s) for s in ("Toronto, ON", "Pricing Analyst (Dover, ON)",
    ...     "Clayton VIC", "Clayton VIC 3168", "Dover, ON L7L 1A1", "Cape Town",
    ...     "Perth, Western Australia", "Dover, British Columbia")]
    [True, True, True, True, True, True, True, True]
    >>> [non_us_place(s) for s in ("Perth, WA", "Dover, ON-SITE", "Raleigh, NC 27703",
    ...     "Concord, North Carolina", "Hands on", "Victoria, TX", "Indian Trail, NC")]
    [False, False, False, False, False, False, False]
    """
    return bool(_NON_US_NAME_RE.search(segment) or _NON_US_CODE_RE.search(segment))


def us_eligible(location: str | None) -> bool:
    """True unless the location names a non-US region with no US marker."""
    loc = (location or "").lower()
    if not loc:
        return True
    if first_hit(_US_MARKERS, loc, SHORT_REMOTE):
        return True
    if first_hit(_NON_US_REGIONS, loc, SHORT_REMOTE):
        return False
    return True

# --------------------------------------------------------------------------- #
#  The verdict the crawl actually asks for                                     #
# --------------------------------------------------------------------------- #

def geo_mode(location: str | None, description: str | None = "") -> str | None:
    """Classify a posting's geography: "onsite" (configured locality),
    "remote", or None (neither). Onsite wins when a posting is both local
    and remote-friendly — a "Remote; Durham, NC" multi-location posting is
    LOCAL material, not a remote drop. The location FIELD is checked against
    the raw place-token regex (profile [locality]; not NC_RE's per-segment
    reading, which is for a location field). Remote detection goes through
    `remote_signal` above (workforce-context phrases, hard negations) rather
    than a bare token list. A location that names a place ("Alameda", "NY
    office", "Oshkosh Medical Center") is read as that place: only the field
    can make it onsite or remote, because a body's "distributed team" or
    "Durham headquarters" boilerplate says nothing about where the seat is.
    The body decides only when the field names no place (location_unknown),
    so "hybrid from our Durham office" still counts as onsite there.

    >>> geo_mode("Alameda", "We are a distributed team.")
    >>> geo_mode("", "We are a distributed team.")
    'remote'
    >>> geo_mode("2 Locations", "Work from home is supported.")
    'remote'
    >>> geo_mode("Alameda; Remote", "")
    'remote'
    >>> here = config.LOCALITY_SUBSTRINGS[0].title()      # the profile's own place
    >>> geo_mode("Alameda", f"Our {here} headquarters runs hybrid.")
    >>> geo_mode("", f"Hybrid from our {here} office.")
    'onsite'
    """
    unknown = location_unknown(location)
    if _names_place(location or "") or (unknown and _names_place(description or "")):
        return "onsite"
    if unknown:
        return "remote" if remote_signal(location, description) else None
    # A placed field is remote only on its own word; the body (62 phrase
    # scans of ~4 KB) is read just for a veto, so only once the field
    # already says remote: 98,781 rows a harvest (2026-10-08 profile).
    if ((remote_signal(location) or "").startswith("location:")
            and remote_signal(location, description)):
        return "remote"
    return None


def geo_label(row: Mapping[str, object]) -> str:
    """A row's stored geo_mode as printed: "elsewhere" for neither local nor
    remote (None), "?" when the row carries none.

    >>> geo_label({"geo_mode": "remote"}), geo_label({"geo_mode": None}), geo_label({})
    ('remote', 'elsewhere', '?')
    """
    return cast(str, row.get("geo_mode") or ("elsewhere" if "geo_mode" in row else "?"))
