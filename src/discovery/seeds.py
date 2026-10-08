"""
Curated seed companies merged into discovery results.

The LLM that suggests employers for a discovery term has blind spots — it
reliably misses the mid-size employers that anchor a specific region or
niche, however obvious they are to someone who lives there. Seeds are your
override: names you KNOW belong in the roster, resolved exactly like
suggested ones (the shared resolver takes the NAME and finds the ATS
itself), so you never need to know a company's ATS to
seed it.

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

from __future__ import annotations

import re
from src import config

SEED_TRIGGERS: tuple[str, ...] = tuple(
    t.strip().lower() for t in config.DISCOVERY_SEED_TRIGGERS if t.strip()
)


def _matches_term(term: str) -> bool:
    """True if `term` should pull the seeds in. No configured triggers means
    the seeds are unconditional — the common case for a hand-picked list."""
    short_token_len = 3
    if not SEED_TRIGGERS:
        return True
    if not term:
        return False
    t = term.lower()
    for trig in SEED_TRIGGERS:
        if len(trig) <= short_token_len:
            if re.search(rf"\b{re.escape(trig)}\b", t):
                return True
        elif trig in t:
            return True
    return False


def seed_names_for(term: str) -> list[str]:
    """The seed names to merge with the LLM's discovery output, or [] if
    `term` doesn't match a configured trigger."""
    return list(config.DISCOVERY_SEED_NAMES) if _matches_term(term) else []
