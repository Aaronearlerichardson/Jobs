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


def remote_signal_for(job):
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

_US_MARKERS = (
    "us", "u.s", "usa", "united states", "america", "americas",
    "north america", "worldwide", "global", "anywhere", "world",
)

_NON_US_REGIONS = (
    "philippines", "india", "pakistan", "bangladesh", "nigeria", "kenya",
    "south africa", "europe", "emea", "apac", "asia", "africa", "latam",
    "latin america", "south america", "canada", "uk", "united kingdom",
    "ireland", "australia", "new zealand", "germany", "france", "spain",
    "poland", "portugal", "netherlands", "ukraine", "romania", "czech",
    "brazil", "argentina", "mexico", "colombia", "vietnam", "indonesia",
    "china", "japan", "singapore",
)


def _region_match(token, text):
    # Short tokens ("us", "uk", "eu") need word boundaries.
    if token.isalpha() and len(token) <= 3:
        return re.search(rf"\b{re.escape(token)}\b", text) is not None
    return token in text


def us_eligible(location):
    """True unless the location names a non-US region with no US marker."""
    loc = (location or "").lower()
    if not loc:
        return True
    if any(_region_match(t, loc) for t in _US_MARKERS):
        return True
    if any(_region_match(t, loc) for t in _NON_US_REGIONS):
        return False
    return True
