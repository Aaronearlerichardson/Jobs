"""
Curated seed companies merged into discovery results.

The LLM that suggests employers for a discovery term has blind spots — it
reliably misses the mid-size employers that anchor a specific region or
niche, however obvious they are to someone who lives there. Seeds are your
override: names you KNOW belong in the roster, probed exactly like suggested
ones (validate_candidate sweeps every ATS when ats='unknown'), so you never
need to know a company's ATS to seed it.

Everything here is configuration, not code — it lives in your profile:

    [discovery]
    seed_companies = [
        "Example Health",                          # just a name, or
        { name = "Example Labs", notes = "why" },  # a name plus a reminder
    ]
    # Optional. When set, seeds merge in ONLY for discovery terms mentioning
    # one of these (e.g. your region), so a search for something unrelated
    # isn't polluted by them. Omit to always merge.
    seed_triggers = ["portland", "oregon", "pnw"]

Short triggers (<= 3 chars) are word-boundary matched so "nc" can't fire
inside "neuroscience".
"""

import re

import config

_SHORT_TOKEN_LEN = 3


SEED_COMPANIES: list[dict] = config.DISCOVERY_SEED_COMPANIES
SEED_TRIGGERS: tuple[str, ...] = tuple(
    t.strip().lower() for t in config.DISCOVERY_SEED_TRIGGERS if t.strip()
)


def _matches_term(term: str) -> bool:
    """True if `term` should pull the seeds in. No configured triggers means
    the seeds are unconditional — the common case for a hand-picked list."""
    if not SEED_TRIGGERS:
        return True
    if not term:
        return False
    t = term.lower()
    for trig in SEED_TRIGGERS:
        if len(trig) <= _SHORT_TOKEN_LEN:
            if re.search(rf"\b{re.escape(trig)}\b", t):
                return True
        elif trig in t:
            return True
    return False


def seed_candidates_for(term: str) -> list[dict]:
    """
    Return raw candidate dicts to merge with the LLM's discovery output, or
    [] if `term` doesn't match a configured trigger.

    Dicts have the same shape as the LLM payload entries, so they flow
    through candidate_from_dict / validate_candidate unchanged.
    """
    if not SEED_COMPANIES or not _matches_term(term):
        return []
    return [
        {
            "name":        s["name"],
            "ats":         "unknown",     # probed against every ATS
            "slug_guess":  None,
            "careers_url": "",
            "notes":       f"[seed] {s['notes']}".strip(),
        }
        for s in SEED_COMPANIES
    ]
