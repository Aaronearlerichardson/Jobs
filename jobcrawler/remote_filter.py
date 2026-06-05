"""Remote-eligibility detection.

Self-contained, side-effect-free module used by the remote-focused track
(``track_remote_neural.py``). It does NOT touch ``config`` or the shared
location filter, so it can be added/removed without disturbing the local
(onsite) crawl path or a parallel track.

A posting is "remote-eligible" if its *location* field or its *body* text
advertises remote / distributed / work-from-anywhere / US-remote work,
and nothing in the text hard-negates that (e.g. "this role is not
remote", "on-site only", "relocation required").

Two precision rules keep false positives down — important because the
remote-neural track surfaces these for human review before emailing:

  * In the body, "distributed" and "anywhere" only count in a *workforce*
    context ("distributed team", "work from anywhere"). A bare
    "distributed" in a body almost always means "distributed systems /
    distributed training", which is ubiquitous in ML roles and says
    nothing about where you sit.
  * In the location field — which is short and curated by the ATS — a bare
    "remote" / "distributed" / "anywhere" token is trusted as a signal.
"""

import re

# Location-field signals. The location string is short and ATS-curated
# ("Remote", "Remote, US", "Remote - United States", "Distributed"), so a
# bare token here is a reliable signal.
_LOC_REMOTE_TOKENS = (
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

# Body signals. Stricter than the location field: "distributed" / "anywhere"
# must appear in a workforce phrase, never bare, to avoid matching
# "distributed systems", "distributed training", "anywhere from 5-10 years".
_BODY_REMOTE_PHRASES = (
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

# Hard negations. If any of these appear, the posting is treated as NOT
# remote-eligible regardless of stray "remote" mentions. Conservative by
# design — the track prefers to drop a borderline posting over emailing a
# false positive.
_HARD_NEGATIONS = (
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

# "wfh" needs word boundaries so it doesn't match inside other tokens.
_WFH_RE = re.compile(r"\bwfh\b")


def _has_token(text, tokens):
    for tok in tokens:
        if tok == "wfh":
            if _WFH_RE.search(text):
                return tok
        elif tok in text:
            return tok
    return None


def remote_signal(location, description=""):
    """Return the phrase that marks this posting remote-eligible, or None.

    Returned phrase is handy for the precision sanity-check sample output.
    """
    loc = (location or "").lower()
    body = (description or "").lower()

    # A hard negation anywhere vetoes the posting.
    if _has_token(loc + " \n " + body, _HARD_NEGATIONS):
        return None

    hit = _has_token(loc, _LOC_REMOTE_TOKENS)
    if hit:
        return f"location:{hit}"

    hit = _has_token(body, _BODY_REMOTE_PHRASES)
    if hit:
        return f"body:{hit}"

    return None


def is_remote_eligible(location, description=""):
    """True iff the posting advertises remote-eligible work."""
    return remote_signal(location, description) is not None
