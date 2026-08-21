"""Snowball sourcing: mine ALREADY-STORED job descriptions for the names of
OTHER organizations -- partners, parents, acquirers, investors, clients,
collaborating institutions -- that never appear in any directory or search
query because they aren't the posting company.

Why this source exists: every other name source in discovery/ is a fixed
list re-derived identically each run (profile seed lists, one directory URL,
a handful of search queries, a 7-day-cached LLM brainstorm) -- a full sweep
of all of them can legitimately return zero new names once the list is
exhausted. A job description is different: it is prose written by the
posting company ABOUT its own ecosystem, so it routinely names the adjacent
employers a directory curator never thought to list ("in partnership with
Acme Diagnostics", "backed by Foo Ventures", "a subsidiary of Bar Health").
Mining descriptions already in the `jobs` table costs zero new HTTP requests.

Precision over recall by design: the existing paste-ingestion path
(discovery/local_sourcing.py add_names) can afford to be permissive because
every name is verified by a live board probe before it's stored -- a junk
guess just fails to resolve. This pass has no such backstop (it only
REPORTS candidates; nothing here is upserted into `companies`), and the
failure mode demonstrated by that same paste path in production ("Home",
"My Network", "Create cover letter" all got as far as store rows) is exactly
what unfiltered free text produces. So extraction here requires BOTH a
name-shaped span (capitalized words, optionally with a corporate suffix)
AND textual evidence it names a distinct organization (a
third-party-introducing phrase, or a corporate-suffix word), and every
candidate is then run through a blocklist tuned for job-posting boilerplate
before it's ever surfaced.
"""

import argparse
import re
import sys
from collections import defaultdict

import config
from core.store import connect, get_companies

# --------------------------------------------------------------------------- #
#  Name normalization (same key everywhere else in discovery/) --------------- #
# --------------------------------------------------------------------------- #

_NONALNUM_RE = re.compile(r"[^a-z0-9]")


def _norm_key(name):
    """Comparison key: strip everything but [a-z0-9], lowercase.

    Matches the key discovery/local_sourcing.py and config.py's blocklist
    already use, so a name already tracked under any spelling/punctuation is
    recognized as the same company.

    >>> _norm_key("Iris Diagnostics, Inc.")
    'irisdiagnosticsinc'
    >>> _norm_key(" Foo-Bar!! ") == _norm_key("foobar")
    True
    >>> _norm_key(None)
    ''
    """
    return _NONALNUM_RE.sub("", (name or "").lower())


# --------------------------------------------------------------------------- #
#  Extraction: phrase-anchored (high precision) + suffix-anchored (recall)    #
# --------------------------------------------------------------------------- #

# One capitalized "word" of a candidate name: starts uppercase, allows
# internal caps/digits/&/'/- ("BioNTech", "R&D", "O'Reilly", "23andMe" is not
# matched by this exact form but common enough spans are).
_WORD = r"[A-Z][A-Za-z0-9&'\-]*"
# A name span: 1-5 such words, with a bare "&" allowed as a connector so a
# name like "Merck Sharp & Dohme" -- where "&" sits alone between spaces and
# so can't match _WORD's "starts with a letter" rule -- stays one span
# instead of breaking into a fragment ("Dohme") at the ampersand.
_NAME_SPAN = rf"{_WORD}(?:\s+(?:{_WORD}|&)){{0,4}}"

# Corporate suffixes: the single strongest standalone signal a span names a
# company rather than a common capitalized phrase ("Our Mission", "The
# Team"). Biotech/health-tech skews heavily toward these over "Inc"/"LLC".
_CORP_SUFFIXES = (
    "Inc", "LLC", "Ltd", "Corp", "Corporation", "Co", "Company", "Group",
    "Holdings", "Partners", "Ventures", "Capital", "Therapeutics",
    "Biosciences", "Biotech", "Bio", "Pharmaceuticals", "Pharma",
    "Diagnostics", "Genomics", "Sciences", "Health", "Healthcare",
    "Medical", "Medicine", "Labs", "Laboratories", "Technologies", "Tech",
    "Systems", "Solutions", "Institute", "Foundation", "University",
    "Hospital", "Clinic", "Network",
)
_SUFFIX_ANCHORED_RE = re.compile(
    rf"\b({_NAME_SPAN}(?:,?\s+(?:{'|'.join(_CORP_SUFFIXES)})\b\.?(?:\s+"
    rf"(?:{'|'.join(_CORP_SUFFIXES)})\b\.?)?))"
)

# Phrases that introduce a THIRD-PARTY organization -- these are the
# strongest evidence, strong enough that a plain 1-2 word capitalized span
# right after one still counts (no suffix required): "partnered with Acme",
# "backed by Foo Capital", "a subsidiary of Bar Health". Each group captures
# the name span that follows the cue.
_INTRO_PHRASES = (
    r"(?:in\s+)?partner(?:s|ed|ship)?\s+with",
    r"in\s+collaboration\s+with",
    r"collaborat(?:e|es|ed|ing)\s+with",
    r"(?:strategic\s+)?alliance\s+with",
    r"backed\s+by",
    r"(?:our\s+)?investors?\s+(?:include|includes|are|is)",
    r"(?:a\s+)?(?:wholly[\s-]owned\s+)?subsidiary\s+of",
    r"(?:a\s+)?(?:portfolio\s+)?compan(?:y|ies)\s+of",
    r"(?:a\s+)?division\s+of",
    r"acquired\s+by",
    r"(?:was\s+)?acquired\s+(?:by\s+)?",
    r"(?:recently\s+)?merged\s+with",
    r"(?:our\s+)?parent\s+company(?:,|\s+is|\s+of)?",
    r"licensed\s+(?:to|from)",
    r"works?\s+closely\s+with",
    r"(?:our\s+)?clients?\s+(?:include|includes|such\s+as)",
    r"customers?\s+(?:include|includes|such\s+as)",
    r"(?:including|such\s+as)\s+companies\s+like",
)
# A run of comma/and-separated name spans right after an intro phrase
# ("clients such as Acme, Foo Inc, and Bar Health"). Captured as ONE group
# built only from _NAME_SPAN + separator characters -- never raw sentence
# text -- so a trailing lowercase clause can never be swept into the
# candidate the way naively slicing raw text past the match would.
_LIST_SPAN = (rf"{_NAME_SPAN}(?:\s*,\s*{_NAME_SPAN})*"
              rf"(?:\s*,?\s+and\s+{_NAME_SPAN})?")
# The cue phrase is matched case-insensitively via a SCOPED inline flag
# ((?i: ... ), Python 3.6+), not a global re.I on the whole pattern: a global
# flag would also case-fold _NAME_SPAN's `[A-Z]` requirement, letting
# ordinary lowercase sentence continuations match as if they were
# capitalized name words.
_INTRO_RE = re.compile(
    rf"(?:(?i:{'|'.join(_INTRO_PHRASES)}))\s+({_LIST_SPAN})"
)

# Split an intro-phrase list capture into its individual name spans.
_LIST_SPLIT_RE = re.compile(r",\s*(?:and\s+)?|\s+and\s+")


def _split_list(span):
    return [s.strip(" .,") for s in _LIST_SPLIT_RE.split(span) if s.strip(" .,")]


# Common sentence-initial / filler words that a suffix- or list-anchored
# match can accidentally sweep in front of the real name. Company names
# essentially never start with these, so stripping trades a vanishingly
# small amount of recall for a real precision win.
_LEADING_FILLER_WORDS = {
    "the", "our", "this", "that", "these", "those", "a", "an", "your",
    "their", "his", "her", "its", "my", "join", "we", "as", "about", "at",
    "visit", "view", "see", "when", "if", "since", "with", "and", "or",
    "please", "read", "carefully", "important", "notice", "attention",
    "note", "job", "description",
}


def _strip_leading_filler(name):
    words = name.split()
    while len(words) > 1 and words[0].lower() in _LEADING_FILLER_WORDS:
        words = words[1:]
    return " ".join(words)


def _collapse_repeated_run(name):
    """Collapse a name that is itself a repeated run of words down to one
    copy of the shortest repeating unit.

    Real posting text sometimes runs a heading straight into the body with
    no separator ("About Advocate Health\xa0 Advocate Health offers..."),
    which the up-to-5-word name span reads as one doubled name. A genuine
    org name is never its own exact repetition, so collapsing loses no real
    signal:

    >>> _collapse_repeated_run("Advocate Health Advocate Health")
    'Advocate Health'
    >>> _collapse_repeated_run("Wake Forest University")
    'Wake Forest University'
    """
    words = name.split()
    n = len(words)
    for period in range(1, n // 2 + 1):
        if n % period == 0 and all(words[i] == words[i % period] for i in range(n)):
            return " ".join(words[:period])
    return name


def extract_candidates(text):
    """Candidate org names in one description, tagged with evidence kind.

    'intro' means the name followed a third-party-introducing phrase (the
    strongest signal, no corporate suffix required); 'suffix' means a bare
    corporate-suffix span was found anywhere in the text (weaker alone,
    since a suffix word can appear in generic prose). Returns a list of
    (name, kind) tuples, not yet filtered against the employer's own name or
    the blocklist -- see `is_plausible_org` and `harvest_from_store`.

    >>> extract_candidates("We are partnered with Acme Devworks on this.")
    [('Acme Devworks', 'intro')]
    >>> extract_candidates("Regular prose with no company mentions at all.")
    []

    A cue phrase followed by a corp-suffix span is caught by BOTH regexes
    -- the intro hit (stronger) and a duplicate suffix hit -- which is fine:
    duplicates collapse downstream in `harvest_from_store`, one per posting.

    >>> for hit in extract_candidates("Backed by Bessemer Ventures and Foo Capital."):
    ...     print(hit)
    ('Bessemer Ventures', 'intro')
    ('Foo Capital', 'intro')
    ('Bessemer Ventures', 'suffix')
    ('Foo Capital', 'suffix')

    A bare corporate-suffix span is recovered even with no intro phrase,
    tagged 'suffix' so downstream ranking can weight it lower:

    >>> extract_candidates("We integrate with Acme Therapeutics products.")
    [('Acme Therapeutics', 'suffix')]
    """
    text = text or ""
    out = []
    for m in _INTRO_RE.finditer(text):
        for name in _split_list(m.group(1)):
            name = _collapse_repeated_run(_strip_leading_filler(name))
            if name:
                out.append((name, "intro"))
    for m in _SUFFIX_ANCHORED_RE.finditer(text):
        name = _collapse_repeated_run(
            _strip_leading_filler(m.group(1).rstrip(".,")))
        if name:
            out.append((name, "suffix"))
    return out


# --------------------------------------------------------------------------- #
#  Filtering -- precision backstop                                            #
# --------------------------------------------------------------------------- #

# Job-board / aggregator / ATS platform names: these show up constantly in
# postings ("Apply on LinkedIn", "powered by Greenhouse") but are never the
# adjacent employer the task is after.
_AGGREGATORS = {
    "linkedin", "indeed", "glassdoor", "ziprecruiter", "monster", "handshake",
    "greenhouse", "lever", "ashby", "workday", "icims", "bamboohr",
    "smartrecruiters", "jobvite", "taleo", "successfactors", "breezy",
    "builtin", "wellfound", "angellist", "themuse", "simplyhired",
    "careerbuilder", "dice", "hired", "getro",
}

# Benefits/insurance/retirement providers: named constantly in the standard
# "our benefits include..." paragraph, never a partner or parent company.
_BENEFITS_PROVIDERS = {
    "aetna", "cigna", "unitedhealthcare", "unitedhealth", "anthem",
    "bluecross", "bluecrossblueshield", "blueshield", "kaiserpermanente",
    "kaiser", "metlife", "guardian", "guardianlife", "principal",
    "principalfinancial", "fidelity", "vanguard", "empower", "adp", "adpco",
    "voya", "prudential", "sunlife", "hartford", "thehartford", "aflac",
    "carefirst", "humana", "cvshealth", "cvscaremark", "expressscripts",
    "healthequity", "lincolnfinancial", "modernhealth",
}

# "Named to X's Best Workplaces"/"as seen in Y" media-recognition
# boilerplate: real publication names, but never the adjacent employer the
# task is after.
_MEDIA_BOILERPLATE = {"fastcompany", "forbes", "glassdoor"}

# EEO/legal-boilerplate fragments: "Equal Opportunity Employer", "Office of
# Federal Contract Compliance Programs" etc. read as name-shaped multi-word
# capitalized spans (with a "suffix" hit on "Program"/"Commission") but are
# never a company. Matched as substrings of the normalized key so partial
# renderings ("EEO Commission", "the Commission") still get caught.
_EEO_BOILERPLATE = {
    "equalopportunityemployer", "equalemploymentopportunity",
    "affirmativeaction", "americanswithdisabilitiesact", "adaamendmentsact",
    "titlevii", "eeoc", "equalemploymentopportunitycommission",
    "officeoffederalcontractcomplianceprograms", "ofccp",
    "familymedicalleaveact", "fmla", "immigrationreformandcontrolact",
}

# Generic capitalized phrases that are name-shaped but never an org: section
# headers, pronoun-led boilerplate, and words the corp-suffix regex latches
# onto in generic prose ("Our Company", "The Group", "Health Care Team").
_GENERIC_WORDS = {
    "ourcompany", "thecompany", "thisrole", "theteam", "ourteam", "thegroup",
    "ourgroup", "aboutus", "whoweare", "whatwedo", "ourmission",
    "ourbenefits", "aboutthecompany", "aboutthisrole", "thecommission",
    "thehealthcareteam", "healthcareteam", "acompanyof", "peopleofcolor",
    "communitiesofcolor", "personsofcolor", "veteransofallages",
}

_BLOCKLIST_KEYS = (_AGGREGATORS | _BENEFITS_PROVIDERS | _EEO_BOILERPLATE
                   | _GENERIC_WORDS | _MEDIA_BOILERPLATE
                   | config.DISCOVERY_NAME_BLOCKLIST)

# A bare corporate-suffix word with nothing but a determiner/pronoun in front
# ("The Company", "Our Group") is exactly the false-positive shape the
# suffix regex is most prone to; require at least one OTHER capitalized word
# ahead of the suffix, i.e. reject spans of 2 words or fewer where word 1 is
# one of these fillers.
_FILLER_LEAD_WORDS = {"the", "our", "this", "a", "an", "your", "their"}


def _looks_like_filler(name):
    words = name.split()
    return len(words) <= 2 and words[0].lower() in _FILLER_LEAD_WORDS


# Bare common nouns that the 'intro' path can mint out of collective
# language ("backed by investors", "collaborating with researchers") with no
# multi-word structure to anchor on.
_BARE_COMMON_NOUNS = {
    "investors", "researchers", "physicians", "clinicians", "scientists",
    "partners", "clients", "customers", "hospitals", "universities",
    "institutions", "companies", "organizations", "company", "group",
    "team", "division", "department", "organization", "commission",
    "program",
}

# Corporate/department/subject-area nouns and adjectives that show up
# constantly in job-posting prose ("Partner with Engineering, Quality...",
# "Collaborate with Quality, Manufacturing, and Engineering teams") but name
# a FUNCTION or FIELD, not an organization. A cue phrase like "partner with"
# or "collaborate with" is written for external orgs but fires just as
# readily on an internal cross-functional team list, and a corp-suffix word
# like "Health"/"Medicine"/"Systems" is itself one of these generic nouns
# when nothing else in the span narrows it down. High posting-frequency on
# a word from this set is evidence the word is common, not evidence it
# names a company.
_GENERIC_CATEGORY_WORDS = {
    "health", "quality", "manufacturing", "product", "information", "life",
    "general", "nuclear", "medicine", "medical", "biological", "computer",
    "system", "systems", "science", "sciences", "maintenance", "control",
    "controls", "business", "unit", "controller", "delinquency",
    "prevention", "juvenile", "justice", "knowledge", "relevant", "tools",
    "technical", "development", "operations", "operation", "sales",
    "marketing", "finance", "financial", "legal", "compliance", "security",
    "analytics", "data", "clinical", "regulatory", "validation",
    "commercial", "privacy", "facilities", "procurement", "technology",
    "technologies", "management", "resources", "human", "supply", "chain",
    "assurance", "affairs", "solutions", "group", "network", "services",
    "support", "administration", "education", "training", "safety",
    "risk", "strategy", "planning", "design", "research", "academic",
    "public", "emergency", "career", "category", "senior", "process",
    "engineering", "it", "hr", "diagnostics", "diagnostic", "commission",
    "directors", "director", "leadership", "success", "champions",
    "regulation", "performance", "owners", "classification", "assistance",
    "plan", "deductible", "vehicles", "temporary", "audit", "managers",
    "manager", "analysts", "analyst", "liaison", "nurses", "staff",
    "utilities", "mobile", "diem", "per", "research", "strategic", "title",
}

# Words that occur as a bare capitalized token in ALL-CAPS section headers
# and bulleted requirement lists ("KNOWLEDGE OF RELEVANT TOOLS AND
# SYSTEMS") -- when one of these lands as a NON-leading word inside a
# candidate span, the span is a mangled sentence fragment rather than a
# name (real org names essentially never contain a bare "Of"/"And"/"The" as
# an internal word; "&" is excluded on purpose since it IS how real names
# like "Merck Sharp & Dohme" join words).
_INFIX_STOPWORDS = {
    "of", "and", "the", "for", "with", "in", "on", "to", "from", "or",
    "as", "by", "at", "is", "are",
}


def _is_generic_bare_phrase(name):
    """True if every word in `name` is a generic corporate/department/
    subject-area noun (see `_GENERIC_CATEGORY_WORDS`) or corporate-suffix
    word, i.e. nothing in the span could distinguish one organization from
    another.

    >>> _is_generic_bare_phrase("Health")
    True
    >>> _is_generic_bare_phrase("Nuclear Medicine")
    True
    >>> _is_generic_bare_phrase("Iris Diagnostics")
    False
    """
    words = [w.lower().strip(".,'") for w in name.split()]
    if not words:
        return False
    suffix_words = {s.lower() for s in _CORP_SUFFIXES}
    return all(w in _GENERIC_CATEGORY_WORDS or w in suffix_words for w in words)


def _is_bare_word_without_suffix(name):
    """True if `name` is a single word (no internal space) with no
    corporate suffix -- the shape almost every internal team/role/function
    name has when a "partner with"/"collaborate with" cue phrase (written
    for external orgs) sweeps up an internal list instead ("Collaborate
    with Quality, Manufacturing, and Engineering teams" mints "Quality",
    "Manufacturing", "Engineering" as if each were a company). A genuine
    third-party org named in a list is essentially always multi-word or
    suffixed ("Iris Diagnostics", "Bessemer Ventures"), so this trades a
    little recall on real single-word company names for a lot of precision
    against internal-list noise -- consistent with this module's stated
    precision-over-recall design.

    >>> _is_bare_word_without_suffix("Quality")
    True
    >>> _is_bare_word_without_suffix("Oracle Inc")
    False
    >>> _is_bare_word_without_suffix("Open AI")
    False
    """
    if " " in name.strip():
        return False
    suffix_words = {s.lower() for s in _CORP_SUFFIXES}
    return name.strip(".,").lower() not in suffix_words


def _has_infix_stopword(name):
    """True if a word after the first is a sentence-glue stopword (see
    `_INFIX_STOPWORDS`), the signature of a mangled sentence fragment
    rather than a name.

    >>> _has_infix_stopword("Knowledge Of Relevant Tools And Systems")
    True
    >>> _has_infix_stopword("Merck Sharp & Dohme")
    False
    """
    words = name.split()
    return any(w.lower() in _INFIX_STOPWORDS for w in words[1:])


def is_plausible_org(name, employer_key):
    """True if `name` survives every precision filter.

    `employer_key` is the normalized name (see `_norm_key`) of the company
    that POSTED the job the candidate came from -- a description mentioning
    its own employer ("Join Acme Health's growing team") is not a NEW
    company:

    >>> is_plausible_org("Acme Health", _norm_key("Acme Health Analytics"))
    False
    >>> is_plausible_org("Iris Diagnostics", _norm_key("Acme Health Analytics"))
    True

    Job-board aggregators, benefits providers, and EEO/legal boilerplate are
    rejected regardless of employer:

    >>> is_plausible_org("LinkedIn", "")
    False
    >>> is_plausible_org("Equal Opportunity Employer", "")
    False
    >>> is_plausible_org("Aetna", "")
    False

    A bare determiner-led phrase ("The Company", "Our Team") is rejected;
    a bare common noun with no other evidence ("investors") is too, even
    though neither is on the fixed blocklists above:

    >>> is_plausible_org("The Company", "")
    False
    >>> is_plausible_org("investors", "")
    False

    A span built entirely from generic corporate/department words --
    including a single corp-suffix word left over after filler-stripping
    ("The Health" -> "Health"), or a department list swept in by a
    "partner/collaborate with" cue meant for external orgs -- is rejected
    for having no word that distinguishes one organization from another:

    >>> is_plausible_org("Health", "")
    False
    >>> is_plausible_org("Nuclear Medicine", "")
    False
    >>> is_plausible_org("Quality", "")
    False

    So is a mangled sentence fragment carrying a bare "Of"/"And"/etc. as an
    internal word (the signature of an ALL-CAPS bullet heading getting
    matched as if it were a name):

    >>> is_plausible_org("Knowledge Of Relevant Tools And Systems", "")
    False
    """
    n = (name or "").strip()
    if not (3 < len(n) <= 60):
        return False
    if not re.search(r"[A-Za-z]{2}", n):
        return False
    key = _norm_key(n)
    if not key:
        return False
    # Not just an exact match: a corp-suffix span can latch onto a
    # TRUNCATED form of the employer's own multi-word name ("Acme Health
    # Analytics is an Equal Opportunity Employer" yields the suffix-anchored
    # "Acme Health" -- the regex stops at "Health" because "Analytics" isn't
    # a known suffix word). Either direction of containment is the
    # employer, not a third party.
    if employer_key and (key == employer_key
                         or employer_key.startswith(key)
                         or key.startswith(employer_key)):
        return False
    if key in _BLOCKLIST_KEYS:
        return False
    if any(key.startswith(b) or b in key for b in _EEO_BOILERPLATE):
        return False
    if _looks_like_filler(n):
        return False
    if " " not in n and n.lower() in _BARE_COMMON_NOUNS:
        return False
    if _is_generic_bare_phrase(n):
        return False
    if _has_infix_stopword(n):
        return False
    return True


# --------------------------------------------------------------------------- #
#  Harvest: descriptions -> ranked, deduped candidates                        #
# --------------------------------------------------------------------------- #

# Evidence weight for an 'intro' hit (explicit third-party-introducing
# phrase, e.g. "partnered with Acme") -- deliberately large relative to a
# bare 'suffix' hit, since a suffix word floating in generic prose is much
# weaker proof on its own (see _GENERIC_CATEGORY_WORDS above for how often
# it's a department/field word, not a company).
_INTRO_POSTING_WEIGHT = 6.0
_SUFFIX_POSTING_WEIGHT = 1.0
_HIGH_FIT_MULTIPLIER = 1.3


def _evidence_score(name, intro_postings, suffix_postings, high_fit):
    """Rank score for one candidate: cue-phrase ('intro') evidence
    dominates, and for a short (<=2 word) name with NO intro evidence, raw
    posting-count is dampened (square root) instead of counted linearly --
    appearing in many postings is what a common word does, not what a
    third-party organization does, so it must not outscore a genuinely rare
    but cue-anchored name.

    >>> _evidence_score("Iris Diagnostics", intro_postings=3, suffix_postings=0, high_fit=False)
    18.0

    A short suffix-only name racks up far more raw postings (79) than a
    genuine 3-posting intro hit, yet still ranks below it:

    >>> generic = _evidence_score("Health", intro_postings=0, suffix_postings=79, high_fit=False)
    >>> real = _evidence_score("Iris Diagnostics", intro_postings=3, suffix_postings=0, high_fit=False)
    >>> generic < real
    True

    A multi-word (3+) suffix-only name is assumed to already carry enough
    of its own specificity that raw frequency is counted normally:

    >>> _evidence_score("Thermo Fisher Scientific", intro_postings=0, suffix_postings=52, high_fit=False)
    52.0
    """
    word_count = len((name or "").split())
    intro_component = intro_postings * _INTRO_POSTING_WEIGHT
    if word_count <= 2 and intro_postings == 0:
        suffix_component = suffix_postings ** 0.5
    else:
        suffix_component = suffix_postings * _SUFFIX_POSTING_WEIGHT
    score = intro_component + suffix_component
    if high_fit:
        score *= _HIGH_FIT_MULTIPLIER
    return round(score, 2)


def _merge_variants(hits):
    """Merge candidates whose display name is a contiguous prefix or
    suffix (at word boundaries) of another candidate's display name,
    summing their evidence into whichever variant was independently seen
    in the most postings.

    One entity often surfaces under two spans -- "the parent Foo Health
    System" in one posting, plain "Foo Health" truncated by the suffix
    regex in another -- which would otherwise split one real
    organization's evidence across two weaker candidates. The MOST
    corroborated variant keeps its display name rather than the longest
    one winning automatically: a 5-word span can be long because it is a
    fuller name, or because a job title or ATS boilerplate line got glued
    onto a short real name by the word-span cap, and posting count (not
    length) is what tells those two apart.

    >>> hits = {
    ...     "carolinasmedical": {"display": "Carolinas Medical", "mentions": 1,
    ...                          "postings": {1}, "intro_postings": set(),
    ...                          "suffix_postings": {1},
    ...                          "high_fit": False, "titles": []},
    ...     "atriumhealthscarolinasmedical": {
    ...         "display": "Atrium Health's Carolinas Medical", "mentions": 1,
    ...         "postings": {2}, "intro_postings": set(),
    ...         "suffix_postings": {2}, "high_fit": False,
    ...         "titles": []},
    ... }
    >>> merged = _merge_variants(hits)
    >>> len(merged)
    1
    >>> next(iter(merged.values()))["display"]
    "Atrium Health's Carolinas Medical"
    >>> next(iter(merged.values()))["postings"] == {1, 2}
    True

    A junk prefix glued onto a well-corroborated short name by the 5-word
    span cap ("MRI Technologist- Duke Regional Hospital", seen once) does
    NOT displace the independently-corroborated "Duke Regional Hospital"
    (seen many times) as the display form, even though it's longer:

    >>> hits2 = {
    ...     "dukeregionalhospital": {
    ...         "display": "Duke Regional Hospital", "mentions": 90,
    ...         "postings": set(range(90)), "intro_postings": set(),
    ...         "suffix_postings": set(range(90)), "high_fit": False,
    ...         "titles": []},
    ...     "mritechnologistdukeregionalhospital": {
    ...         "display": "MRI Technologist- Duke Regional Hospital",
    ...         "mentions": 1, "postings": {90}, "intro_postings": set(),
    ...         "suffix_postings": {90}, "high_fit": False, "titles": []},
    ... }
    >>> next(iter(_merge_variants(hits2).values()))["display"]
    'Duke Regional Hospital'
    """
    items = sorted(hits.items(),
                   key=lambda kv: (-len(kv[1]["postings"]),
                                   -len(kv[1]["display"].split())))
    consumed = set()
    merged = {}
    for key, h in items:
        if key in consumed:
            continue
        words = h["display"].lower().split()
        for key2, h2 in items:
            if key2 == key or key2 in consumed or key2 in merged:
                continue
            words2 = h2["display"].lower().split()
            if not words2 or words2 == words:
                continue
            shorter, longer = ((words2, words) if len(words2) < len(words)
                               else (words, words2))
            if not (longer[:len(shorter)] == shorter
                   or longer[-len(shorter):] == shorter):
                continue
            h["mentions"] += h2["mentions"]
            h["postings"] |= h2["postings"]
            h["intro_postings"] |= h2["intro_postings"]
            h["suffix_postings"] |= h2["suffix_postings"]
            h["high_fit"] = h["high_fit"] or h2["high_fit"]
            for t in h2["titles"]:
                if len(h["titles"]) < 3 and t not in h["titles"]:
                    h["titles"].append(t)
            consumed.add(key2)
        merged[key] = h
    return merged


def harvest_from_store(conn, min_mentions=2, min_score=None, use_llm=False,
                       limit=None):
    """Mine every stored job description for third-party organization names
    not already in the company roster.

    Returns a ranked list of dicts: {name, mentions, postings, high_fit,
    score, sample_titles}, highest `score` first. Nothing is written to the
    store -- this is a report; downstream resolution (probing the candidate
    for a real board and adding it) is a separate, human-triggered pass. See
    tests/test_snowball.py for the ranking and dedup behavior verified
    end-to-end against a fixture store.

    `min_mentions` drops singleton sightings (one posting's one-off
    phrasing is the likeliest false positive; two independent postings
    naming the same organization is real corroboration). `min_score`
    additionally floors the evidence-weighted score. `use_llm` runs the
    optional core.claude refinement pass over the surviving heuristic
    candidates -- see `_llm_refine`; off by default, and this function never
    reaches the network unless it is explicitly set.
    """
    existing = {_norm_key(r["name"]) for r in get_companies(conn, active_only=False)}
    rows = conn.execute(
        "SELECT job_id, company_name, title, description, resume_fit_score "
        "FROM jobs WHERE description IS NOT NULL AND description != ''"
    ).fetchall()

    # name-key -> accumulated evidence
    hits = defaultdict(lambda: {"display": None, "mentions": 0, "postings": set(),
                                "intro_postings": set(), "suffix_postings": set(),
                                "high_fit": False, "titles": []})
    for r in rows:
        employer_key = _norm_key(r["company_name"])
        # One evidence kind per (candidate, posting): the intro loop inside
        # extract_candidates runs before the suffix loop, so when both fire
        # for the same span in the same posting, 'intro' -- the stronger
        # signal -- wins rather than being diluted by a duplicate 'suffix'
        # hit on the same sentence.
        seen_this_posting = {}
        for name, kind in extract_candidates(r["description"]):
            if not is_plausible_org(name, employer_key):
                continue
            if kind == "intro" and _is_bare_word_without_suffix(name):
                continue
            key = _norm_key(name)
            if key in existing or key in seen_this_posting:
                continue
            seen_this_posting[key] = kind
            h = hits[key]
            # Prefer the longest surface form seen as the display name (more
            # likely to be the full "Foo Therapeutics" over a partial "Foo"
            # caught by a different sentence).
            if not h["display"] or len(name) > len(h["display"]):
                h["display"] = name
            h["mentions"] += 1
            h["postings"].add(r["job_id"])
            (h["intro_postings"] if kind == "intro" else h["suffix_postings"]).add(r["job_id"])
            fit = r["resume_fit_score"]
            if fit is not None and fit >= 0.7:
                h["high_fit"] = True
            if len(h["titles"]) < 3 and r["title"] not in h["titles"]:
                h["titles"].append(r["title"])

    hits = _merge_variants(hits)

    out = []
    for key, h in hits.items():
        n_postings = len(h["postings"])
        if n_postings < min_mentions:
            continue
        score = _evidence_score(h["display"], len(h["intro_postings"]),
                                len(h["suffix_postings"]), h["high_fit"])
        if min_score is not None and score < min_score:
            continue
        out.append({
            "name": h["display"], "mentions": h["mentions"],
            "postings": n_postings, "high_fit": h["high_fit"],
            "score": score, "sample_titles": h["titles"],
        })
    out.sort(key=lambda d: (d["score"], d["postings"]), reverse=True)
    if use_llm and out:
        out = _llm_refine(out)
    if limit:
        out = out[:limit]
    return out


def _llm_refine(candidates):
    """Optional second pass: ask Claude to drop anything in `candidates`
    that isn't really a distinct organization (catches shapes the regex
    heuristics can't, e.g. a person's name with a corporate-suffix-looking
    trailing word).

    Notes:
        Off by default and never called from the CLI without --llm. Fails
        open, not closed, on purpose: an LLM outage or missing API key
        should never zero out an otherwise-valid heuristic report, so any
        error or empty response returns `candidates` unchanged rather than
        `[]`.
    """
    from core.claude import call_claude_json
    system = (
        "You are cleaning a list of candidate organization names auto-extracted "
        "from job-posting text via regex. Some entries are real distinct "
        "companies/institutions (partners, parents, acquirers, investors, "
        "clients). Some are noise: job titles, benefits providers, EEO/legal "
        "phrases, generic phrases, or the posting company's own name. Return "
        'JSON {"keep": ["name", ...]} containing ONLY the names from the '
        "input list that are real, distinct organizations. Do not add or "
        "rename anything."
    )
    names = [c["name"] for c in candidates]
    user = "Candidate names:\n" + "\n".join(f"- {n}" for n in names)
    try:
        data = call_claude_json(system, user, max_tokens=2000)
    except Exception as e:
        print(f"    [!] LLM refine failed ({type(e).__name__}: {e}); "
              f"keeping the heuristic list unfiltered")
        return candidates
    keep = data.get("keep") if isinstance(data, dict) else None
    if not keep:
        return candidates
    keep_keys = {_norm_key(n) for n in keep}
    return [c for c in candidates if _norm_key(c["name"]) in keep_keys]


# --------------------------------------------------------------------------- #
#  Report + CLI                                                               #
# --------------------------------------------------------------------------- #

def _print_safe(line):
    """print() a line, tolerating a console codepage (cp1252 on a default
    Windows shell) that can't encode every character real posting text
    contains -- a narrow no-break space (U+202F) is common enough to have
    crashed a real run outright. CI always sets PYTHONUTF8=1, so this path
    is otherwise untested there; the fallback degrades to '?' substitution
    rather than raising.

    Notes:
        Not covered by a doctest: doctest's captured stdout doesn't carry a
        real console encoding, so there's nothing for a narrow codepage
        assertion to exercise here.
    """
    try:
        print(line)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, errors="replace").decode(enc))


def print_report(candidates):
    w = 66
    print = _print_safe
    print(f"\n{'='*w}")
    print("  Snowball: company names mined from stored job descriptions")
    print(f"{'='*w}")
    if not candidates:
        print("  (no new candidates above the mention threshold)")
    for c in candidates:
        fit_flag = "  [high-fit posting]" if c["high_fit"] else ""
        print(f"    {c['name'][:34]:34} score={c['score']:<5} "
              f"postings={c['postings']:<3}{fit_flag}")
        if c["sample_titles"]:
            print(f"        e.g. {'; '.join(c['sample_titles'])}")
    print(f"{'='*w}")
    print(f"  {len(candidates)} candidate(s). Not written to the company "
          f"store -- resolve/verify separately before adding.\n")


def run_snowball(min_mentions=2, min_score=None, use_llm=False, limit=None,
                 db_path=None):
    """Callable entry point (also used by tests): connect, harvest, report,
    and return the candidate list."""
    conn = connect(db_path or config.STORE_DB_PATH)
    try:
        candidates = harvest_from_store(conn, min_mentions=min_mentions,
                                        min_score=min_score, use_llm=use_llm,
                                        limit=limit)
    finally:
        conn.close()
    print_report(candidates)
    return candidates


def main():
    ap = argparse.ArgumentParser(
        description="Mine stored job descriptions for third-party company "
                    "names (partners/parents/acquirers/investors/clients) "
                    "not already in the company roster. Reports only -- "
                    "nothing is written to the store.")
    ap.add_argument("--min-mentions", type=int, default=2,
                    help="Drop candidates seen in fewer than this many "
                         "distinct postings (default: 2)")
    ap.add_argument("--min-score", type=float, default=None,
                    help="Drop candidates below this evidence-weighted score")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap the number of candidates reported")
    ap.add_argument("--llm", action="store_true",
                    help="Optional Claude refinement pass over the heuristic "
                         "list (requires ANTHROPIC_API_KEY; off by default)")
    args = ap.parse_args()
    run_snowball(min_mentions=args.min_mentions, min_score=args.min_score,
                use_llm=args.llm, limit=args.limit)


if __name__ == "__main__":
    main()
