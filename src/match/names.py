"""Company-name tokenizing shared by every discovery path.

One place for the ways a company name is reduced to something matchable --
a comparison key, domain-token guesses, ATS-slug guesses -- and the one
corporate-suffix list they all strip. The slug probes, careers-page sniffer,
Workday probe, pipeline slug variants, snowball harvester and store dedup
all used to carry their own copy of these, each with its own stopword set.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Corporate suffixes stripped when guessing a domain or slug from a name: the
# domain rarely carries them (redhat.com, not redhatinc.com), and ATS slugs
# are far more often the head word than the full legal name. Field-flavoured
# words ("therapeutics", "biosciences") count too, deliberately -- see
# strip_suffixes.
COMPANY_SUFFIXES = frozenset({
    "inc", "incorporated", "corp", "corporation", "ltd", "llc", "co",
    "company", "technologies", "systems", "therapeutics", "biosciences",
    "pharmaceuticals", "pharma", "sciences", "bio", "labs", "group",
    "health", "holdings",
})


def strip_parentheticals(name: str | None) -> str:
    """`name` without any (...) or [...] group, an unbalanced one included.

    >>> strip_parentheticals("Acme Corp (NC office)")
    'Acme Corp'
    >>> strip_parentheticals("Acme [YC W20] (Series A")
    'Acme'
    >>> strip_parentheticals(None)
    ''
    """
    # The closing bracket is optional so an unbalanced group still comes off.
    return re.sub(r"\s*[\(\[][^)\]]*[\)\]]?", "", name or "")


def name_key(name: str | None) -> str:
    """Comparison key: lowercase, everything but [a-z0-9] dropped, so one
    company under any spelling or punctuation keys the same.

    >>> name_key("Iris Diagnostics, Inc.")
    'irisdiagnosticsinc'
    >>> name_key(" Foo-Bar!! ") == name_key("foobar")
    True
    >>> name_key(None)
    ''

    Notes:
        src.store._name_key and config.DISCOVERY_NAME_BLOCKLIST compute
        the same key (core and config cannot import discovery), so a name
        blocked or rejected under any spelling stays recognised here.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# The roster `source` of rows named from their board's slug and nothing
# else: src.discovery.dork.harvest_urls titles the slug ("aah" -> "Aah")
# because a search hit carries no employer name.
SLUG_NAME_SOURCE = "ats_dork"


def name_is_own_slug(name: str | None, slug: str | None) -> bool:
    """True when `name` is nothing but its own board's slug/tenant, spelled
    out -- a roster row named "Xyz" after its Workday tenant "xyz" rather
    than the employer's real name, which a name taken from the URL leaves
    behind (src.ats.coords.board_slug reads the slug/tenant off a row).

    Compared via name_key, so case and punctuation never matter, and a
    slug with the name's words simply joined together still counts as
    "the same":

    >>> name_is_own_slug("Xyz", "xyz")
    True
    >>> name_is_own_slug("Bigco", "bigco")
    True
    >>> name_is_own_slug("Acme Health", "acmehealth")
    True

    A real name that merely CONTAINS its slug, or shares no relation to
    it, is not:

    >>> name_is_own_slug("Xavier Young Health", "xyz")
    False
    >>> name_is_own_slug("Acme Health Systems", "acme")
    False

    Neither is a name with nothing to compare, either side blank:

    >>> name_is_own_slug("Acme", ""), name_is_own_slug("", "acme")
    (False, False)

    Notes:
        On its own this cannot tell a slug-derived name from a real
        one-word name that happens to equal its slug: on 2026-09-17 it
        matched 321 of 579 harvestable rows, most of them correctly
        named. Restricted to SLUG_NAME_SOURCE rows (183) and ordered by
        board size, the head of the list is the raw tenant codes worth
        renaming, which is how the HARVEST SUMMARY uses it. A name that
        is a PREFIX of its slug (a tenant "<name>depot" named "<Name>")
        is not caught; a prefix rule would flag every truncated name.
    """
    key = name_key(name)
    return bool(key and slug and key == name_key(slug))


def name_words(name: str | None) -> list[str]:
    """The lowercase alphanumeric words of `name`, parentheticals dropped.

    >>> name_words("Bio-Signal Technologies, Inc. (Durham)")
    ['bio', 'signal', 'technologies', 'inc']
    >>> name_words(None)
    []
    """
    return re.findall(r"[a-z0-9]+", strip_parentheticals(name).lower())


def strip_suffixes(name: str | None) -> str:
    """`name` without parentheticals or corporate suffix words.

    >>> strip_suffixes("Corcept Therapeutics (NC office)")
    'Corcept'
    >>> strip_suffixes("United Therapeutics, Inc.")
    'United'
    >>> strip_suffixes("Acme Corp")
    'Acme'

    Punctuation and runs of whitespace left behind by the stripping are
    collapsed, so the result is always a clean single-spaced name:

    >>> strip_suffixes("  Acme  Corp  ")
    'Acme'

    A name with nothing to strip is returned as-is, and a missing name is
    the empty string rather than an error:

    >>> strip_suffixes("Wolfspeed")
    'Wolfspeed'
    >>> strip_suffixes(None)
    ''

    Notes:
        "Therapeutics" and "Biosciences" count as suffixes here even though
        they are part of the legal name. That is deliberate: ATS slugs are
        far more often the head word than the full name, and slug_guesses
        keeps the unstripped form as a candidate anyway.
    """
    # Word-boundary match so "biosciences" does not eat "bio"; the optional
    # dot takes "Inc." with it.
    s = re.sub(r"\b(?:" + "|".join(sorted(COMPANY_SUFFIXES)) + r")\b\.?", "",
               strip_parentheticals(name), flags=re.IGNORECASE)
    s = re.sub(r"[,\.]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def junk_name_reason(name: str | None) -> str:
    """Why `name` is not an employer, or '' when it may be one.

    A screen, not a verdict: it rejects only the shapes that a pasted job
    posting or a scraped listing produces and a real company name never
    does. Every reason is a stable token for a miss_reason qualifier.

    Section headings and requirement fragments:

    >>> junk_name_reason("Required Qualifications")
    'section-heading'
    >>> junk_name_reason("Proficiency in SQL.")
    'section-heading'
    >>> junk_name_reason("Title")
    'section-heading'

    Sentence fragments end in punctuation a name never carries:

    >>> junk_name_reason("Experience with Python and R.")
    'section-heading'
    >>> junk_name_reason("We are hiring:")
    'sentence-fragment'

    A category is not a company:

    >>> junk_name_reason("Oncology")
    'category-only'
    >>> junk_name_reason("Medical Devices")
    'category-only'
    >>> junk_name_reason("Engineering")
    'category-only'

    Nor is a listing's own status ("Retired" from a stale roster scrape) --
    and "Jobs" alone is a section word, not a company either:

    >>> junk_name_reason("Retired")
    'status-only'
    >>> junk_name_reason("Jobs")
    'section-heading'

    Location strings and search-result chrome:

    >>> junk_name_reason("Raleigh-Durham-Chapel Hill Area (On-site)")
    'location-string'
    >>> junk_name_reason("99+ results")
    'listing-chrome'
    >>> [junk_name_reason(n) for n in ("1 benefit", "8 benefits",
    ...     "25Low Match", "3 days ago")]
    ['listing-chrome', 'listing-chrome', 'listing-chrome', 'listing-chrome']

    A bare number or ordinal, and a person's name with credentials:

    >>> junk_name_reason("2nd"), junk_name_reason("12")
    ('number-only', 'number-only')
    >>> junk_name_reason("Jane Q Public, MSHR, PHR")
    'person-credentials'

    A numbered copy of a name ("Fairwai 1", "Luna Physical Therapy 1") is a
    scraper's duplicate marker, not a second employer:

    >>> junk_name_reason("Luna Physical Therapy 1")
    'numbered-duplicate'

    Too short, too long, or empty:

    >>> junk_name_reason("A")
    'too-short'
    >>> junk_name_reason("Senior data engineer to build the pipelines that power our platform")
    'too-long'
    >>> junk_name_reason("")
    'empty'

    Real names pass, including ones that contain a category or section
    word alongside a proper noun, a legal suffix, or a number that is part
    of the name:

    >>> [junk_name_reason(n) for n in ("Beacon Biosignals", "Judi Health",
    ...     "SAS Institute", "Cala Health, Inc.", "3M", "Studio 54",
    ...     "Duke University", "Blue Cross NC", "Q2 Solutions", "IBM")]
    ['', '', '', '', '', '', '', '', '', '']
    >>> [junk_name_reason(n) for n in ("23andMe", "Bio-Techne", "3 Day Blinds",
    ...     "10x Genomics", "Acme, LLC")]
    ['', '', '', '', '']
    """
    # Why a pasted name gets screened at all: a pasted posting yields
    # "Required Qualifications", "Proficiency in SQL." and "Oncology" as
    # readily as it yields the employer, and each one that reaches
    # resolution costs a careers-page sniff (a dozen guessed URLs), two web
    # searches and a mission call (2026-09-01 add-names, 2026-09-02
    # reresolve logs).
    s = (name or "").strip()
    if not s:
        return "empty"
    # A count glued to what it counts, or to a match grade: LinkedIn chrome
    # ("1 benefit", "8 benefits", "25Low Match", "3 days ago") that the
    # 2026-09-22 reresolve spent full resolve cycles on. "Day" alone is not
    # chrome ("3 Day Blinds"), so only "days ago" counts.
    if re.match(r"^\s*\d+\+?\s*(?:results?|jobs?|benefits?|match(?:es)?|days?\s+ago|"
                r"(?:low|medium|high|good|strong)\s+match)\b", s, re.IGNORECASE):
        return "listing-chrome"
    # "2nd", "12": a stray list index or ordinal. Digits WITH letters are
    # names ("3M", "23andMe", "Q2"), so only a bare number is rejected.
    if re.match(r"^\d+(?:st|nd|rd|th)?$", s, re.IGNORECASE):
        return "number-only"
    # A person's name with credential suffixes (", MSHR, PHR"): a
    # recruiter's byline off a pasted posting. Two or more are required,
    # since one is a legal form or a state ("Acme, LLC", "Durham, NC").
    if re.search(r"(?:,\s*[A-Z]{2,5}){2,}\s*$", s):
        return "person-credentials"
    if re.search(r"\barea\b|\(on-?site|\(remote|\(hybrid|\bmetropolitan\b|\bcounty\b",
                 s, re.IGNORECASE):
        return "location-string"
    words = name_words(s)
    if not words or (len(s) < 2):
        return "too-short"
    if len(words) > 7:
        return "too-long"
    # Words that, alone, name a job-posting section rather than an employer.
    section = {
        "qualifications", "qualification", "requirements", "requirement",
        "responsibilities", "responsibility", "proficiency", "proficient",
        "experience", "skills", "skill", "benefits", "salary", "compensation",
        "preferred", "required", "results", "result", "title", "summary",
        "description", "overview", "about", "apply", "applicants", "duties",
        "education", "degree", "years", "location", "remote", "hybrid",
        "onsite", "on-site", "position", "positions", "role", "roles", "job",
        "jobs", "career", "careers", "posted", "ago", "full-time", "part-time",
    }
    if s[-1] in ".:;,!?" and not re.search(
            r"\b(?:inc|co|corp|ltd|llc|plc|sa|ag|gmbh|bv|nv|jr|sr)\.$", s, re.IGNORECASE):
        # A trailing period belongs to a legal abbreviation ("Cala Health,
        # Inc.") or to a sentence; any other end punctuation to a sentence.
        if s[-1] == "." and any(w in section for w in words):
            return "section-heading"
        return "sentence-fragment"
    if all(w in section or w in {"in", "with", "and", "or", "of", "the", "a", "to"}
           for w in words):
        return "section-heading"
    if words[0] in section and len(words) >= 2 and words[1] in (
            section | {"in", "with", "of"}):
        return "section-heading"
    # Category nouns a name may consist of ENTIRELY without naming anyone:
    # "Oncology", "Medical Devices", "Health Care Services".
    if all(w in {
            "oncology", "medical", "devices", "device", "health", "healthcare",
            "care", "services", "service", "biotech", "biotechnology", "pharma",
            "pharmaceutical", "pharmaceuticals", "software", "engineering",
            "research", "clinical", "data", "analytics", "technology",
            "technologies", "university", "hospital", "laboratory", "laboratories",
            "science", "sciences", "life", "solutions", "consulting", "staffing",
            "recruiting", "diagnostics", "therapeutics", "and", "of", "the",
            "group", "team", "company", "companies", "industry", "industries",
            } for w in words):
        return "category-only"
    # A single generic word describing a LISTING's own disposition, never an
    # employer's name: a scraped roster entry that kept only a status column
    # ("Retired") reads exactly like this, the same one-word-is-the-whole-name
    # shape as a category, kept apart so the reason names what it saw.
    if all(w in {"retired", "archived", "inactive", "expired", "discontinued",
                 "disabled", "deprecated", "closed"} for w in words):
        return "status-only"
    m = re.match(r"^(.*\S)\s+\d{1,2}$", s)
    if m and len(words) >= 2 and not re.search(r"\d", m.group(1)) \
            and m.group(1).split()[-1].lower() not in {"studio", "area", "channel", "route", "no", "number"}:
        return "numbered-duplicate"
    return ""


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in items:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def domain_tokens(name: str | None) -> list[str]:
    """Likely domain tokens for `name`, best first: the full joined name,
    then the suffix-stripped joined form, then the bare first word.

    >>> domain_tokens("United Therapeutics")
    ['unitedtherapeutics', 'united']
    >>> domain_tokens("Red Hat Inc")
    ['redhatinc', 'redhat', 'red']
    >>> domain_tokens("Pfizer")
    ['pfizer']
    >>> domain_tokens("")
    []

    Notes:
        The full form leads because "unitedtherapeutics.com" beats the
        ambiguous "united.com"; the first word is a last resort and is
        flagged by risky_domain_tokens.
    """
    words = name_words(name)
    if not words:
        return []
    kept = [w for w in words if w not in COMPANY_SUFFIXES]
    return _dedupe(["".join(words), "".join(kept), kept[0] if kept else words[0]])


def risky_domain_tokens(name: str | None) -> set[str]:
    """The domain_tokens(name) that are a TRUNCATED guess at a multi-word
    company's domain: the bare first word, or a generic word. A hit
    reached only through one of these has no post-hoc job count to
    sanity-check it against, so the fetched page has to
    corroborate the company name (see src.discovery.resolve.identity._corroborates) first.

    >>> sorted(risky_domain_tokens("Galaxy Diagnostics"))
    ['galaxy']
    >>> sorted(risky_domain_tokens("Lindy Biosciences"))
    ['lindy']
    >>> sorted(risky_domain_tokens("United Therapeutics"))
    ['united']

    A single-word name has no "first word of a multi-word name" to be
    truncated to -- it is not a risky domain guess, just the whole name --
    and a parenthetical does not make a name multi-word:

    >>> risky_domain_tokens("Pfizer")
    set()
    >>> risky_domain_tokens("Pfizer (NYC)")
    set()

    Notes:
        "Red Hat Inc" -> stripped "redhat" is NOT flagged: dropping a
        corporate suffix is precise (the domain really does omit "Inc"),
        unlike collapsing a multi-word name down to one ambiguous word.
    """
    words = name_words(name)
    if len(words) < 2:
        return set()
    full = "".join(words)
    # Generic single words that collide with an unrelated DOMAIN when a
    # multi-word name is truncated to one of them: "galaxy.com" for "Galaxy
    # Diagnostics" is a fintech. (slug_guesses never emits a bare first word:
    # "Bio-Signal Technologies" -> "signal" used to hit an unrelated Lever
    # board.)
    return {t for t in domain_tokens(name)
            if t != full and (t == words[0] or t in {
                "signal", "neuro", "neural", "brain", "medical", "health", "data",
                "bio", "tech", "labs", "lab", "systems", "smart", "micro", "nano",
                "bci", "ai", "research", "digital", "care", "vision", "sense"})}


def slug_guesses(name: str | None) -> list[str]:
    """ATS-slug guesses for `name`, in probe order: joined, hyphenated, and
    suffix-stripped-joined.

    >>> slug_guesses("United Imaging Intelligence")
    ['unitedimagingintelligence', 'united-imaging-intelligence']
    >>> slug_guesses("Red Hat, Inc.")
    ['redhatinc', 'red-hat-inc', 'redhat']
    >>> slug_guesses("")
    []

    The bare first word is never added on its own ("eli", "novo",
    "charles" collide with unrelated boards and shadow the real employer);
    only a name that is one word plus suffixes reduces to it:

    >>> slug_guesses("Eli Lilly and Company")
    ['elilillyandcompany', 'eli-lilly-and-company', 'elilillyand']
    >>> slug_guesses("United Therapeutics")
    ['unitedtherapeutics', 'united-therapeutics', 'united']
    """
    words = name_words(name)
    if not words:
        return []
    joined = "".join(words)
    stripped = "".join(w for w in words if w not in COMPANY_SUFFIXES) or joined
    return _dedupe([joined, "-".join(words), stripped])
