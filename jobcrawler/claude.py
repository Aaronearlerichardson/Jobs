"""Claude API wrapper + search-expansion prompts."""

import json
import re

import requests

import config
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL


# --------------------------------------------------------------------------- #
#  Prompt building blocks — the candidate identity + mission taxonomy come    #
#  from profile.toml (via config), so scoring/discovery is about whoever's    #
#  profile is loaded, not a hard-coded person.                                #
# --------------------------------------------------------------------------- #

_CANDIDATE = config.CANDIDATE_SUMMARY or "A technical candidate seeking a targeted job search."
_AVOID = config.CANDIDATE_AVOID or ""

# Mission tiers as loaded (highest alignment → lowest, last is the catch-all).
_MISSION_TIERS = tuple(t["name"] for t in config.MISSION_TIERS) or ("other",)
ACTIVE_MISSION_TIERS = tuple(t["name"] for t in config.MISSION_TIERS if t["active"])

# Compiled bullseye pin (profile [mission].bullseye_regex); None when disabled.
_BULLSEYE_RE = re.compile(config.MISSION_BULLSEYE_REGEX, re.I) \
    if config.MISSION_BULLSEYE_REGEX else None


def _tier_enum():
    """`"name" (desc)` lines for the mission-tier list in prompts."""
    return "\n".join(f'    "{t["name"]}" — {t["desc"]}' for t in config.MISSION_TIERS)


def _tier_bands():
    """`lo-hi = name: desc` score-band lines for the mission prompt."""
    out = []
    for t in config.MISSION_TIERS:
        lo, hi = t["band"]
        out.append(f"    * {lo:.2f}-{hi:.2f} = {t['name']}: {t['desc']}")
    return "\n".join(out)


_EXPAND_SYSTEM = f"""You are a job-search strategist for this candidate:

{_CANDIDATE}
{_AVOID}

Given a job title, skill, or concept, return ONLY a JSON object with exactly three keys:
- "titles": array of up to 12 alternative job-title strings to search for, matched to the candidate's reachable level.
- "keywords": array of up to 12 technical keywords/skills/domain terms that surface more relevant listings.
- "sectors": array of up to 12 company types, industry verticals, or named employers/labs where these roles exist.
Return ONLY valid JSON. No markdown, no explanation, no preamble."""


_TECH_BAR_SCORE_SYSTEM = f"""You are a technical-hiring screener for this candidate:

{_CANDIDATE}

The candidate wants ANY role with a genuine TECHNICAL or QUANTITATIVE component — NOT only machine learning. Given a job posting (title + description), rate the role's TECHNICAL BAR on a 0.0-to-1.0 scale.

Scoring rubric:
- HIGH (0.75-1.0): the core work is hands-on technical — writing software; building or maintaining data pipelines, databases, ETL, or infrastructure; quantitative/statistical analysis; modeling, algorithms, or research; quality, test, validation, or systems engineering; data management or data engineering; bioinformatics/computational work. The person builds, engineers, analyzes, or rigorously tests.
- MEDIUM (0.4-0.7): partially technical — an analyst/specialist who runs existing tools or queries rather than building them, or a role mixing technical tasks with coordination/admin.
- LOW (0.0-0.35): little or no technical component — executing SOPs, coordination/monitoring, paperwork, manual data ENTRY, scheduling, patient care, recruiting, sales, marketing, or general people/project management without technical depth.

Key distinctions: "data management" / "data engineering" / "quality engineering" / "test engineering" / "validation" / "analysis" are TECHNICAL (high-ish). "data ENTRY" / "coordination" / "monitoring" are NOT (low). Judge by the ACTUAL responsibilities, not the title or seniority.

Also classify the employer's MISSION into exactly one tier:
{_tier_enum()}

Return ONLY a JSON object with exactly these keys:
- "score": a number from 0.0 to 1.0 (two decimals) — the TECHNICAL BAR.
- "mission": one of {", ".join(f'"{t}"' for t in _MISSION_TIERS)}.
- "reason": one short phrase (<= 12 words) naming the deciding factor.
Return ONLY valid JSON. No markdown, no preamble."""


_LOCATION_EXPAND_SYSTEM = """You are a geographic search strategist. Given a location term (a city, region, country, or qualifier like "remote"), return ONLY a JSON object with exactly two keys:
- "include": array of up to 15 related location strings that should ALSO match when filtering jobs for this area. Examples: for "North Carolina", include "NC", "Durham", "Raleigh", "Chapel Hill", "Research Triangle", "RTP". For "remote", include "work from home", "wfh", "fully remote", "distributed", "anywhere".
- "exclude": array of up to 8 location strings that should be explicitly excluded when someone specifies this search. Examples: for "us only", include common offshore locations the user likely wants to filter out.
Use lowercase unless the token is normally capitalized (country codes etc). Return ONLY valid JSON, no markdown, no explanation."""


DISCOVER_SYSTEM = f"""You are a technical recruiter who maps employers to ATS platforms. Given a sector, industry, or job concept, list companies that (a) plausibly hire for roles in that space and (b) are likely to post jobs publicly. The candidate you're sourcing for:

{_CANDIDATE}
{_AVOID}

Return ONLY a JSON object with this exact shape:""" + r"""
{
  "companies": [
    {
      "name": "Full company name",
      "ats": "greenhouse" | "lever" | "ashby" | "kula" | "workday" | "unknown",
      "slug_guess": "likely-slug-on-that-ats-or-null",
      "careers_url": "https://…",
      "notes": "One short sentence on why this company fits."
    }
  ],
  "gated_sites": [
    {
      "site": "linkedin" | "indeed" | "builtin" | "wellfound",
      "query": "search query a user could run there",
      "notes": "What makes this site worth the auth hassle for this sector."
    }
  ]
}

Rules:
- Up to 15 companies. Prefer ones with roles the candidate above could realistically land.
- slug_guess: best educated guess (typically the company name lowercased with hyphens). Use null if you really can't guess.
- ats: "unknown" is fine if you're not sure.
- Return ONLY valid JSON. No markdown, no commentary."""


def call_claude_json(system_prompt, user_content, max_tokens=1000):
    """POST to /v1/messages, return the JSON block from the text response."""
    if ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_API_KEY_HERE":
        print("  [!] Set ANTHROPIC_API_KEY env var (or edit config.py).")
        return {}
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "system":     system_prompt,
                "messages":   [{"role": "user", "content": user_content}],
            },
            timeout=60,
        )
        r.raise_for_status()
        text = next(
            (b["text"] for b in r.json().get("content", []) if b.get("type") == "text"),
            "{}",
        )
        cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        return json.loads(cleaned)
    except requests.HTTPError as e:
        body = getattr(e.response, "text", "")[:300]
        print(f"  [!] Claude API error: {e}  body={body!r}")
        return {}
    except json.JSONDecodeError as e:
        print(f"  [!] Claude returned non-JSON: {e}")
        return {}
    except Exception as e:
        print(f"  [!] Claude call failed: {e}")
        return {}


def expand_search(term):
    return call_claude_json(_EXPAND_SYSTEM, term)


def expand_location(term):
    return call_claude_json(_LOCATION_EXPAND_SYSTEM, term)


# --------------------------------------------------------------------------- #
#  Technical-bar scorer (repurposes the --expand Claude call).                 #
#                                                                              #
#  Instead of expanding a term into more keywords, this asks Claude to score   #
#  a single posting 0.0-1.0 on how much real model/algorithm/research work it  #
#  involves (high) vs. SOP-execution / study-coordination / data-entry (low).  #
# --------------------------------------------------------------------------- #

_COMPANY_MISSION_SYSTEM = f"""You score how well an EMPLOYER matches a specific candidate's ideal target, from 0.0 to 1.0. Given a company name + sample postings, judge the COMPANY (not one role).

{_CANDIDATE}

Return ONLY a JSON object with exactly:
- "mission": one of
{_tier_enum()}
- "score": 0.0-1.0 alignment with the candidate's target — pick within the band for the tier you chose:
{_tier_bands()}
- "reason": one short phrase (<= 12 words).
Return ONLY valid JSON. No markdown, no preamble."""


# Résumé-fit scoring moved to jobcrawler/fit.py (multi-axis rubric + gates).
# score_resume_fit() below is a thin delegator; the old single-scalar prompt
# and its _STRENGTHS / _FIT_CAPS blocks were retired with it.


def score_company_mission(name, context=""):
    """Return (mission_tier|None, score|None, reason) for an employer."""
    # Deterministic bullseye anchor (profile [mission].bullseye_regex), checked
    # BEFORE the LLM: a company whose NAME is the candidate's exact target is
    # pinned to 1.0 in the bullseye tier with no API call. The name is the whole
    # signal here, and the mission model often can't see a client-rendered
    # careers page anyway (this is how Science.xyz got mis-scored to 0.10).
    # Match the NAME only, never the reason text, where the model's negations
    # live ("no neurotech focus") and a substring match would invert the result.
    if _BULLSEYE_RE is not None and _BULLSEYE_RE.search(name.lower()):
        return config.MISSION_BULLSEYE_TIER or None, 1.0, "bullseye: named target"
    user = f"COMPANY: {name}\n\nSAMPLE POSTINGS / CONTEXT:\n{(context or '(none)')[:1500]}"
    result = call_claude_json(_COMPANY_MISSION_SYSTEM, user, max_tokens=120)
    if not result or "mission" not in result:
        return None, None, ""
    tier = str(result.get("mission", "")).strip().lower()
    if tier not in _MISSION_TIERS:
        tier = None
    try:
        score = max(0.0, min(1.0, float(result.get("score"))))
    except (TypeError, ValueError):
        score = None
    reason = str(result.get("reason", "")).strip()
    return tier, score, reason


def score_resume_fit(resume, title, description=""):
    """Delegate to the multi-axis rubric in jobcrawler/fit.py; returns a
    FitResult (`.score`, `.axes`, `.gates`, `.reason`, and `.as_columns()` /
    `.as_legacy()`). `resume` is accepted for backward compatibility but the
    rubric scores against the config profile (strengths, domain ladder, stack),
    not raw résumé text. Imported lazily to avoid a claude<->fit import cycle."""
    from jobcrawler import fit
    return fit.score_resume_fit(title, description)


def score_technical_bar(title, description=""):
    """
    Return (score: float in [0,1], reason: str, mission: str|None) for one
    posting, where mission is one of _MISSION_TIERS (None when unknown).

    Falls back to ``(None, "", None)`` when the API key is unset or the call
    fails, so callers can degrade to a heuristic without crashing.
    """
    desc = (description or "")[:2500]
    user = f"TITLE: {title}\n\nDESCRIPTION:\n{desc or '(no description provided)'}"
    result = call_claude_json(_TECH_BAR_SCORE_SYSTEM, user, max_tokens=120)
    if not result or "score" not in result:
        return None, "", None
    try:
        score = float(result["score"])
    except (TypeError, ValueError):
        return None, "", None
    score = max(0.0, min(1.0, score))
    mission = str(result.get("mission", "")).strip().lower()
    if mission not in _MISSION_TIERS:
        mission = None
    return score, str(result.get("reason", "")).strip(), mission
