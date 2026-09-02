"""Company-name tokenizing shared by every discovery path.

One place for the ways a company name is reduced to something matchable --
a comparison key, domain-token guesses, ATS-slug guesses -- and the one
corporate-suffix list they all strip. The slug probes, careers-page sniffer,
Workday probe, pipeline slug variants, snowball harvester and store dedup
all used to carry their own copy of these, each with its own stopword set.
"""

import re

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

# Generic single words that collide with an unrelated board or domain when a
# multi-word name is truncated to one of them: "Bio-Signal Technologies" ->
# the bare slug "signal" hits some unrelated Lever board; "galaxy.com" for
# "Galaxy Diagnostics" is a fintech.
GENERIC_WORDS = frozenset({
    "signal", "neuro", "neural", "brain", "medical", "health", "data",
    "bio", "tech", "labs", "lab", "systems", "smart", "micro", "nano",
    "bci", "ai", "research", "digital", "care", "vision", "sense",
})

# A "(...)" or "[...]" group, the closing bracket optional so an unbalanced
# one still comes off ("Acme (Series A").
_PAREN_RE = re.compile(r"\s*[\(\[][^)\]]*[\)\]]?")
_NONALNUM_RE = re.compile(r"[^a-z0-9]")
_WORD_RE = re.compile(r"[a-z0-9]+")
# Word-boundary match so "biosciences" does not eat "bio"; the optional dot
# takes "Inc." with it.
_SUFFIX_RE = re.compile(
    r"\b(?:" + "|".join(sorted(COMPANY_SUFFIXES)) + r")\b\.?",
    re.IGNORECASE,
)


def strip_parentheticals(name):
    """`name` without any (...) or [...] group, an unbalanced one included.

    >>> strip_parentheticals("Acme Corp (NC office)")
    'Acme Corp'
    >>> strip_parentheticals("Acme [YC W20] (Series A")
    'Acme'
    >>> strip_parentheticals(None)
    ''
    """
    return _PAREN_RE.sub("", name or "")


def name_key(name):
    """Comparison key: lowercase, everything but [a-z0-9] dropped, so one
    company under any spelling or punctuation keys the same.

    >>> name_key("Iris Diagnostics, Inc.")
    'irisdiagnosticsinc'
    >>> name_key(" Foo-Bar!! ") == name_key("foobar")
    True
    >>> name_key(None)
    ''

    Notes:
        core.store._name_key and config.DISCOVERY_NAME_BLOCKLIST compute
        the same key (core and config cannot import discovery), so a name
        blocked or rejected under any spelling stays recognised here.
    """
    return _NONALNUM_RE.sub("", (name or "").lower())


def name_words(name):
    """The lowercase alphanumeric words of `name`, parentheticals dropped.

    >>> name_words("Bio-Signal Technologies, Inc. (Durham)")
    ['bio', 'signal', 'technologies', 'inc']
    >>> name_words(None)
    []
    """
    return _WORD_RE.findall(strip_parentheticals(name).lower())


def strip_suffixes(name):
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
        far more often the head word than the full name, and
        pipeline.slug_variants keeps the unstripped form as a candidate
        anyway.
    """
    s = _SUFFIX_RE.sub("", strip_parentheticals(name))
    s = re.sub(r"[,\.]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _dedupe(items):
    out, seen = [], set()
    for t in items:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def domain_tokens(name):
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


def risky_domain_tokens(name):
    """The domain_tokens(name) that are a TRUNCATED guess at a multi-word
    company's domain: the bare first word, or a generic word from
    GENERIC_WORDS. A hit reached only through one of these has no post-hoc
    job count to sanity-check it against, so the fetched page has to
    corroborate the company name (see sniffer._corroborates) first.

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
    return {t for t in domain_tokens(name)
            if t != full and (t == words[0] or t in GENERIC_WORDS)}


def slug_guesses(name):
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
