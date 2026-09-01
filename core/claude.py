"""Claude API wrapper + search-expansion prompts."""

import atexit
import json
import logging
import os
import re
import threading
import time

import requests

from scrapers.http import SESSION

import config
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

# File-only per-call trace (session log DEBUG channel — never printed).
_log = logging.getLogger("claude")


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


def is_active_mission(tier, name, include_missions=None):
    """The one activation rule: should a newly-sourced company be crawled?

    `tier` is the mission tier from :func:`score_company_mission`, `name` the
    company name, `include_missions` an optional override of the profile's
    active tiers. Returns 1 (crawl it) or 0 (park it) — an int, because it
    goes straight into the ``companies.active`` column.

    A company is active when ANY of these hold:

    * its tier is one of the active tiers,
    * its tier is ``None`` — scoring was UNAVAILABLE, not negative,
    * it is a multi-division conglomerate (profile policy).

    >>> tiers = ("green", "blue")
    >>> is_active_mission("green", "Nowhere Robotics", tiers)
    1
    >>> is_active_mission("red", "Nowhere Robotics", tiers)
    0

    An unavailable score must never read as "off-mission". A failed or
    rate-limited call returns ``(None, None, "")``, and treating that as a
    rejection buries a whole discovery sweep in inactive rows:

    >>> is_active_mission(None, "Nowhere Robotics", tiers)
    1

    Omitting `include_missions` falls back to the profile's active tiers,
    so the answer depends on the loaded profile rather than this literal:

    >>> is_active_mission(ACTIVE_MISSION_TIERS[0], "Nowhere Robotics")
    1

    Notes:
        This lived inline at six call sites (four in discovery/, one in
        scrapers/ops.py, one in discovery/ats_dork.py). The ats_dork copy had
        drifted to two hard-coded tier names and no ``tier is None`` arm,
        which wrote entire sweeps inactive on any API hiccup — and harvest_urls
        skips boards already in the store, so those rows were never re-probed.
        tests/test_invariants.py keeps the rule single-sourced.
    """
    tiers = ACTIVE_MISSION_TIERS if include_missions is None else include_missions
    return 1 if (tier in tiers or tier is None
                 or config.is_multi_division(name)) else 0


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


# 5-family models run ADAPTIVE THINKING when the `thinking` param is omitted,
# and max_tokens caps thinking + response text TOGETHER — an unguarded upgrade
# would let thinking eat a 300-token scoring budget and truncate the JSON.
# For these models we explicitly disable thinking unless the caller opts in.
_THINKING_DEFAULT_MODELS = ("claude-sonnet-5", "claude-opus-5",
                            "claude-fable-5", "claude-mythos-5")


# --------------------------------------------------------------------------- #
#  Prompt caching.                                                             #
#                                                                              #
#  Every call here is the same shape: a big STABLE system prompt (the rubric +  #
#  the candidate profile) plus a small per-posting user turn. A crawl scores    #
#  hundreds of jobs against a byte-identical system prompt, so a cache          #
#  breakpoint at the end of `system` turns that prefix into a 0.1x read after   #
#  the first write (1.25x). Placement is the whole trick: the breakpoint sits   #
#  on the system block, NEVER on the user turn, which differs every request.    #
#                                                                              #
#  Below the model's minimum cacheable prefix (1024 tokens on Sonnet 5, 512 on  #
#  Opus 5) the API silently declines to cache and bills normally — no error and #
#  no premium — so marking every call is safe. As of 2026-08 that means the     #
#  fit screen (~1.5k tokens) and deep verify (~1.8k) cache; the tech-bar and    #
#  mission prompts (~0.7k / ~0.5k) are under Sonnet 5's floor and simply don't. #
#                                                                              #
#  CLAUDE_PROMPT_CACHE=0 disables; CLAUDE_CACHE_TTL=1h buys the 1-hour cache    #
#  (2x writes) for runs whose calls are spread more than 5 minutes apart.       #
# --------------------------------------------------------------------------- #

_CACHE_ENABLED = os.environ.get("CLAUDE_PROMPT_CACHE", "1").lower() \
    not in ("0", "false", "no", "off")
_CACHE_TTL = os.environ.get("CLAUDE_CACHE_TTL", "5m").strip().lower()

# Scoring runs inside ThreadPoolExecutor pools, and a cache entry is only
# readable once the first response has started — N workers firing at once
# would each pay a full-price write of the same prefix. The first caller for a
# given (model, system) claims the prefix and the rest wait for it to land, so
# the pool pays one write and N-1 reads. Bounded: if the leader hangs or errors
# the followers go ahead anyway and just miss the cache.
_GATE_WAIT_S = 90
_GATE_LOCK = threading.Lock()
_PREFIX_GATES = {}

# Cumulative token accounting, so cache behaviour is observable rather than
# assumed (a silent invalidator shows up here as cache_read stuck at 0).
_USAGE_LOCK = threading.Lock()
_USAGE = {"calls": 0, "uncached_input": 0, "cache_write": 0,
          "cache_read": 0, "output": 0}


# Minimum cacheable prefix per model (Anthropic docs, 2026-08). Not monotonic
# across generations — Opus 5 caches from 512 tokens, Sonnet 5 needs 1024,
# Haiku 4.5 needs 4096 — so a prompt that caches on the verify model may
# silently not cache on the screen model. Informational: we mark every call
# regardless (below the floor the API just doesn't cache, at no extra cost).
_CACHE_MIN_TOKENS = {
    "claude-opus-5": 512, "claude-fable-5": 512, "claude-mythos-5": 512,
    "claude-opus-4-8": 1024, "claude-sonnet-5": 1024, "claude-sonnet-4-6": 1024,
    "claude-opus-4-7": 2048,
    "claude-opus-4-6": 4096, "claude-haiku-4-5": 4096,
}


def min_cacheable_tokens(model=None):
    """Smallest prefix the given model will cache; 1024 if unknown."""
    name = model or CLAUDE_MODEL
    for prefix, floor in _CACHE_MIN_TOKENS.items():
        if name.startswith(prefix):
            return floor
    return 1024


def _system_field(system_prompt, cache=True):
    """`system` as a cache-marked block list, or the plain string when caching
    is off. One breakpoint, on the last (only) system block — that covers the
    whole tools->system prefix and leaves the varying user turn uncached."""
    if not (cache and _CACHE_ENABLED and system_prompt):
        return system_prompt
    control = {"type": "ephemeral"}
    if _CACHE_TTL == "1h":
        control["ttl"] = "1h"
    return [{"type": "text", "text": system_prompt, "cache_control": control}]


def build_payload(system_prompt, user_content, max_tokens=1000,
                  model=None, thinking=False, cache=True):
    """The /v1/messages request body. Split out from the POST so the payload
    shape (cache breakpoint placement, thinking guard) is testable offline."""
    use_model = model or CLAUDE_MODEL
    payload = {
        "model":      use_model,
        "max_tokens": max_tokens,
        "system":     _system_field(system_prompt, cache),
        "messages":   [{"role": "user", "content": user_content}],
    }
    if not thinking and use_model.startswith(_THINKING_DEFAULT_MODELS):
        payload["thinking"] = {"type": "disabled"}
    return payload


def _claim_prefix(model, system_prompt):
    """First caller for this prefix leads (returns the Event it must set when
    its request finishes); everyone else waits for the leader, then proceeds.
    Returns (event_to_set_or_None)."""
    key = (model, hash(system_prompt))
    with _GATE_LOCK:
        event = _PREFIX_GATES.get(key)
        if event is None:
            event = threading.Event()
            _PREFIX_GATES[key] = event
            return event
    event.wait(timeout=_GATE_WAIT_S)
    return None


def _record_usage(usage):
    with _USAGE_LOCK:
        _USAGE["calls"] += 1
        _USAGE["uncached_input"] += int(usage.get("input_tokens") or 0)
        _USAGE["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
        _USAGE["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
        _USAGE["output"] += int(usage.get("output_tokens") or 0)


def cache_stats():
    """Cumulative token counters for this process (a plain dict copy)."""
    with _USAGE_LOCK:
        return dict(_USAGE)


def format_cache_stats():
    """One-line summary. `hit` is the share of the CACHEABLE prefix served from
    cache — 0% across a whole run with a large system prompt means something is
    invalidating the prefix (a timestamp in it, a changed model, a changed
    profile mid-run)."""
    s = cache_stats()
    cached = s["cache_read"] + s["cache_write"]
    hit = (100.0 * s["cache_read"] / cached) if cached else 0.0
    total_in = cached + s["uncached_input"]
    return (f"  [claude] {s['calls']} call(s) | input {total_in:,} tok "
            f"(cache read {s['cache_read']:,}, wrote {s['cache_write']:,}, "
            f"uncached {s['uncached_input']:,}; {hit:.0f}% of cacheable prefix hit) "
            f"| output {s['output']:,} tok")


@atexit.register
def _print_cache_stats_at_exit():
    if _USAGE["calls"] and os.environ.get("CLAUDE_USAGE_SUMMARY", "1") != "0":
        print(format_cache_stats())


# Unrecoverable-API-error circuit breaker. Some failures can never succeed on
# retry within the same run — an exhausted credit balance (400), a bad or
# revoked API key (401/403). Without a breaker each job's call fails
# independently: the 2026-08-31 rescore burned 973 consecutive "credit balance
# is too low" 400s over five minutes before finishing. Once tripped, every
# later call in the process returns {} immediately without touching the API.
_FATAL_LOCK = threading.Lock()
_FATAL_MSG = None

# Transient statuses worth one short retry ladder (529 = overloaded_error).
_RETRY_STATUSES = (429, 500, 502, 503, 529)
_RETRY_DELAYS = (2.0, 8.0)


def _trip_fatal(msg):
    global _FATAL_MSG
    with _FATAL_LOCK:
        if _FATAL_MSG is None:
            _FATAL_MSG = msg
            print(f"  [!] Claude API disabled for the rest of this run "
                  f"(unrecoverable): {msg}")


def call_claude_json(system_prompt, user_content, max_tokens=1000,
                     model=None, thinking=False, cache=True):
    """POST to /v1/messages, return the JSON block from the text response.

    `model` overrides config.CLAUDE_MODEL for this call (the deep-verify
    pass runs a stronger model than the screen). `thinking=True` leaves the
    model's default adaptive thinking on (5-family models) — pair it with a
    max_tokens large enough for thinking + the JSON; the default False
    pins thinking off so small structured calls can't be truncated by it.
    `cache=False` opts this call out of the system-prompt cache breakpoint
    (see the prompt-caching block above); the default is on everywhere."""
    if ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_API_KEY_HERE":
        print("  [!] Set ANTHROPIC_API_KEY env var (or edit config.py).")
        return {}
    if _FATAL_MSG is not None:
        _log.debug("claude call skipped (breaker tripped): %s", _FATAL_MSG)
        return {}
    use_model = model or CLAUDE_MODEL
    payload = build_payload(system_prompt, user_content, max_tokens,
                            use_model, thinking, cache)
    lead = _claim_prefix(use_model, system_prompt) \
        if (cache and _CACHE_ENABLED and system_prompt) else None
    try:
        for attempt in range(len(_RETRY_DELAYS) + 1):
            r = SESSION.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json=payload,
                timeout=120,
            )
            if r.status_code in _RETRY_STATUSES and attempt < len(_RETRY_DELAYS):
                try:
                    delay = float(r.headers.get("retry-after", ""))
                except ValueError:
                    delay = _RETRY_DELAYS[attempt]
                _log.debug("claude %s -> retrying in %.0fs (attempt %d)",
                           r.status_code, delay, attempt + 1)
                time.sleep(min(delay, 60.0))
                continue
            break
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        _record_usage(usage)
        _log.debug("%s: %s in / %s out tokens (cache read: %s)",
                   use_model, usage.get("input_tokens"),
                   usage.get("output_tokens"),
                   usage.get("cache_read_input_tokens", 0))
        text = next(
            (b["text"] for b in data.get("content", []) if b.get("type") == "text"),
            "",
        )
        cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        if not cleaned:
            # Two "no answer" shapes that aren't parse errors: adaptive
            # thinking exhausting max_tokens before any text lands
            # (stop_reason max_tokens — raise the caller's budget), and a
            # safety refusal (stop_reason refusal — no text block at all).
            print(f"  [!] Claude returned no text "
                  f"(stop_reason={data.get('stop_reason')})")
            return {}
        # strict=False: the model occasionally emits a literal newline/tab
        # INSIDE a JSON string value ("reason": "...line one
        # line two..."), which strict json.loads rejects as "Invalid
        # control character" and cost a company its mission score in the
        # 2026-08-28 discover-local session. Lenient parsing reads it fine.
        return json.loads(cleaned, strict=False)
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        body = getattr(e.response, "text", "")[:300]
        if status in (401, 403) or (status == 400
                                    and "credit balance" in body.lower()):
            _trip_fatal(f"HTTP {status}: {body!r}")
        else:
            print(f"  [!] Claude API error: {e}  body={body!r}")
        return {}
    except json.JSONDecodeError as e:
        print(f"  [!] Claude returned non-JSON: {e}")
        return {}
    except Exception as e:
        print(f"  [!] Claude call failed: {e}")
        return {}
    finally:
        # Release any threads waiting on this prefix — including on failure,
        # so one bad request can't stall a scoring pool for _GATE_WAIT_S.
        if lead is not None:
            lead.set()


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


# Résumé-fit scoring moved to core/fit.py (multi-axis rubric + gates).
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


_BOARD_OWNER_SYSTEM = (
    "You judge job-board ownership. Given a company name and evidence about "
    "the ATS board its name resolved to (tenant/slug tokens, the board's "
    "display name, sample job titles), decide whether that board is the "
    "company's OWN hiring board — including former names, rebrands and "
    "merged identities (Merck & Co. posts on Workday tenant 'msd') — or a "
    "DIFFERENT organization's board: a parent conglomerate's shared board "
    "(Genedata linking to Danaher's 'danaher'/'DanaherJobs'), or an "
    "unrelated company that happens to hold a colliding short slug "
    "(greenhouse 'ripple' is Ripple the payments company, not Ripple "
    "Neuro). Sample job titles reveal the board's real industry — weigh "
    "them heavily when given. "
    'Reply with JSON only: {"same_employer": true|false, "reason": "<one line>"}'
)

_BOARD_OWNER_CACHE = {}


def board_is_own(company, board, site="", titles=()):
    """True/False: is `board` (a Workday tenant, or "<ats>:<slug>")
    `company`'s own hiring board? None when the API is unavailable or the
    reply is malformed — callers keep the hit on None (offline behavior
    unchanged) and only reject on a clear False. Verdicts are cached per
    (company, board) for the process.

    Notes:
        Consulted only for collision-prone resolutions — a Workday tenant
        sharing no token with the name (discovery.sniffer._foreign_board),
        or a first-word/generic slug probe hit (discovery.pipeline) — so
        this costs a call on the rare suspect, not per resolve. The
        asymmetric default matters: a wrong "keep" mislabels one company
        until a human looks, a wrong "reject" silently loses a real board
        forever. `titles` (sample postings from the board) is the decisive
        evidence for slug collisions.
    """
    key = (str(company).lower(), str(board).lower())
    if key in _BOARD_OWNER_CACHE:
        return _BOARD_OWNER_CACHE[key]
    user = f"COMPANY: {company}\nBOARD: {board}"
    if site:
        user += f"\nBOARD DISPLAY NAME / SITE: {site}"
    if titles:
        user += "\nSAMPLE JOB TITLES: " + " | ".join(
            t for t in list(titles)[:8] if t)
    result = call_claude_json(_BOARD_OWNER_SYSTEM, user, max_tokens=150)
    verdict = (bool(result["same_employer"])
               if result and "same_employer" in result else None)
    if verdict is not None:
        _BOARD_OWNER_CACHE[key] = verdict
    return verdict


def score_resume_fit(resume, title, description=""):
    """Delegate to the multi-axis rubric in core/fit.py; returns a
    FitResult (`.score`, `.axes`, `.gates`, `.reason`, and `.as_columns()` /
    `.as_legacy()`). `resume` is accepted for backward compatibility but the
    rubric scores against the config profile (strengths, domain ladder, stack),
    not raw résumé text. Imported lazily to avoid a claude<->fit import cycle."""
    from core import fit
    return fit.score_resume_fit(title, description)


def score_technical_bar(title, description=""):
    """
    Return (score: float in [0,1], reason: str, mission: str|None) for one
    posting, where mission is one of _MISSION_TIERS (None when unknown).

    Falls back to ``(None, "", None)`` when the API key is unset or the call
    fails, so callers can degrade to a heuristic without crashing.
    """
    from core.fit import clip_desc
    desc = clip_desc(description or "")
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
