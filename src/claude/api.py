"""Claude API wrapper + search-expansion prompts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Iterable
from typing import Annotated, Any

from pydantic import StrictBool, ValidationError

from src import config, runstate
from src.claude.reply import Reply, Unit
from src.net import http
from src.validation import OneOf

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

# The activation rule and the tier set it reads live in src/config/policy.py
# now, beside `is_multi_division`, which the rule calls. Nothing about
# either is an LLM concern -- they are a profile table and a
# None-means-unknown convention -- and keeping them here made src/store
# import the LLM module to default one column, which was the last thing
# stopping store from depending on config alone.
#
# Re-exported under the names every call site and comment already uses.
# The rule itself is single-sourced; tests/test_invariants.py enforces that
# no caller reimplements it.
ACTIVE_MISSION_TIERS = config.ACTIVE_MISSION_TIERS
is_active_mission = config.is_active_mission


def _tier_bands() -> str:
    """`lo-hi = name: desc` score-band lines for the mission prompt."""
    out = []
    for t in config.MISSION_TIERS:
        lo, hi = t["band"]
        out.append(f"    * {lo:.2f}-{hi:.2f} = {t['name']}: {t['desc']}")
    return "\n".join(out)


class ExpandReply(Reply):
    titles: list[str]
    keywords: list[str]
    sectors: list[str]


class LocationReply(Reply):
    include: list[str]
    exclude: list[str]


#: The platforms the model may name: those discovery reaches from a company
#: name alone, by a guessed handle (`guess`) or one read off its careers
#: pages (`discovery.scan`).
_ATS_GUESSES = (*(ats for ats, spec in config.BOARDS.items()
                  if spec.get("guess") or spec.get("discovery", {}).get("scan")),
                "unknown")

DISCOVER_SYSTEM = f"""You are a technical recruiter who maps employers to ATS platforms. Given a sector, industry, or job concept, list companies that (a) plausibly hire for roles in that space and (b) are likely to post jobs publicly. The candidate you're sourcing for:

{_CANDIDATE}
{_AVOID}

Return ONLY a JSON object with this exact shape:""" + r"""
{
  "companies": [
    {
      "name": "Full company name",
      "ats": """ + " | ".join(f'"{a}"' for a in _ATS_GUESSES) + r""",
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
- ats: "unknown" is fine if you're not sure."""


_GATED_SITES = ("linkedin", "indeed", "builtin", "wellfound")


class DiscoveredCompany(Reply):
    name: str
    ats: Annotated[str, OneOf(_ATS_GUESSES, loose=True)]
    slug_guess: str | None
    careers_url: str
    notes: str


class GatedSite(Reply):
    site: Annotated[str, OneOf(_GATED_SITES, loose=True)]
    query: str
    notes: str


class DiscoverReply(Reply):
    companies: list[DiscoveredCompany]
    gated_sites: list[GatedSite]


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
#  fit screen (~1.5k tokens) and deep verify (~1.8k) cache; the mission         #
#  prompt (~0.5k) is under Sonnet 5's floor and simply doesn't.                 #
#                                                                              #
#  CLAUDE_PROMPT_CACHE=0 disables; CLAUDE_CACHE_TTL=1h buys the 1-hour cache    #
#  (2x writes) for runs whose calls are spread more than 5 minutes apart.       #
# --------------------------------------------------------------------------- #

# Scoring fans out (every call a coroutine on the event loop), and a cache
# entry is only readable once the first response has started — N calls firing
# at once would each pay a full-price write of the same prefix. The first
# caller for a given (model, system) claims the prefix and the rest wait for
# it to land, so the fan-out pays one write and N-1 reads. Bounded: if the
# leader hangs or errors the followers go ahead anyway and just miss the cache.
#
# Token accounting keeps cache behaviour observable rather than assumed (a
# silent invalidator shows up as cache_read stuck at 0).


class _Calls:
    """What a run's Claude calls share: the prompt-cache gates (model,
    hash(system)) -> asyncio.Event (see _claim_prefix); the token counters,
    and those the last footer covered (see report_cache_stats), printed
    as the run ends when the claude_usage_summary setting is on; the
    breaker's message once tripped (see _trip_fatal); and the board-owner
    checks it started that are still in flight, which the run's end waits
    for, config.CLAUDE_OWNER_WAIT_S at most, before its session closes and
    its footer prints (see board_is_own)."""

    def __init__(self) -> None:
        self.gates: dict[tuple[str, int], asyncio.Event] = {}
        self.usage = dict.fromkeys(("calls", "uncached_input", "cache_write",
                                    "cache_read", "output"), 0)
        self.reported = dict(self.usage)
        self.fatal: str | None = None
        self.asking: set[asyncio.Future[bool | None]] = set()
        if config.SETTINGS.claude_usage_summary:
            runstate.at_exit(report_cache_stats)
        runstate.at_exit(self._land_asks)

    async def _land_asks(self) -> None:
        if self.asking:
            await asyncio.wait(self.asking, timeout=config.CLAUDE_OWNER_WAIT_S)


_CALLS = runstate.per_run(_Calls)


def _system_field(system_prompt: str, cache: bool = True) -> str | list[dict[str, Any]]:
    """`system` as a cache-marked block list, or the plain string when caching
    is off. One breakpoint, on the last (only) system block — that covers the
    whole tools->system prefix and leaves the varying user turn uncached."""
    if not (cache and config.SETTINGS.claude_prompt_cache and system_prompt):
        return system_prompt
    control: dict[str, str] = {"type": "ephemeral"}
    if config.SETTINGS.claude_cache_ttl == "1h":
        control["ttl"] = "1h"
    return [{"type": "text", "text": system_prompt, "cache_control": control}]


def build_payload(system_prompt: str, user_content: str, max_tokens: int = 1000,
                  model: str | None = None, thinking: bool = False, cache: bool = True, *,
                  reply: type[Reply] | None = None) -> dict[str, Any]:
    """The /v1/messages request body. Split out from the POST so the payload
    shape (cache breakpoint placement, thinking guard, reply schema) is
    testable offline.

    `reply` (a Reply subclass) holds the answer to its schema: structured
    outputs (`output_config.format`), or a forced tool call on a legacy
    model (one without structured outputs).

    Notes:
        Structured outputs add their own system prompt, and changing the
        format invalidates the prompt cache; each system prompt here always
        travels with the same reply shape, so the cached prefix stays
        stable. Thinking is compatible: the grammar constrains only the
        final text, not the thinking blocks (the verify pass relies on it).
    """
    # 5-family models think when `thinking` is omitted, and max_tokens caps
    # thinking + response text TOGETHER, so thinking could eat a 300-token
    # scoring budget and truncate the JSON. When the caller does not ask for
    # thinking (docs build-with-claude/thinking-troubleshooting, 2026-09):
    # these accept {"type": "disabled"} (Opus 5 only at effort high or below,
    # its default, which build_payload leaves alone)...
    thinking_optional = ("claude-sonnet-5", "claude-opus-5")
    # ...and these always think and reject "disabled" with a 400, so they get
    # no `thinking` field, effort "low", and room for that thinking on top of
    # the caller's max_tokens. Matched first: "claude-opus-5" is a prefix of
    # "claude-opus-5-5".
    thinking_always = ("claude-opus-5-5", "claude-fable-5", "claude-mythos-5")
    # Models without structured outputs (absent from the feature's
    # supportedModels, docs build-with-claude/structured-outputs, 2026-09).
    # They get the reply as a forced tool call instead; none of them thinks
    # unless asked, and build_payload never asks, so forcing is always allowed.
    legacy = ("claude-3", "claude-opus-4-0", "claude-opus-4-1", "claude-opus-4-2025",
              "claude-sonnet-4-0", "claude-sonnet-4-2025")
    use_model = model or config.CLAUDE_MODEL
    payload: dict[str, Any] = {
        "model":      use_model,
        "max_tokens": max_tokens,
        "system":     _system_field(system_prompt, cache),
        "messages":   [{"role": "user", "content": user_content}],
    }
    output: dict[str, Any] = {}
    if not thinking:
        if use_model.startswith(thinking_always):
            # max_tokens is the only cap on their thinking (budget_tokens
            # 400s too), and callers size it for the reply alone (120-300
            # tokens for a score): headroom for low-effort thinking, billed
            # only as used and bounded by config.CLAUDE_TIMEOUT.
            payload["max_tokens"] += config.CLAUDE_THINKING_HEADROOM
            output["effort"] = "low"
        elif use_model.startswith(thinking_optional):
            payload["thinking"] = {"type": "disabled"}
    if reply is not None:
        schema = reply.model_json_schema()
        if use_model.startswith(legacy):
            payload["tools"] = [{"name": reply.__name__, "input_schema": schema}]
            payload["tool_choice"] = {"type": "tool", "name": reply.__name__}
        else:
            output["format"] = {"type": "json_schema", "schema": schema}
    if output:
        payload["output_config"] = output
    return payload


async def _claim_prefix(model: str, system_prompt: str) -> asyncio.Event | None:
    """The first caller for this prefix leads: it gets the Event to set when
    its request finishes. Everyone else waits for the leader,
    config.CLAUDE_GATE_WAIT_S at most, and gets None."""
    key, gates = (model, hash(system_prompt)), _CALLS().gates
    event = gates.get(key)
    if event is None:
        event = gates[key] = asyncio.Event()
        return event
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(config.CLAUDE_GATE_WAIT_S):
            await event.wait()
    return None


def _record_usage(usage: dict[str, Any]) -> None:
    u = _CALLS().usage
    u["calls"] += 1
    u["uncached_input"] += int(usage.get("input_tokens") or 0)
    u["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
    u["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
    u["output"] += int(usage.get("output_tokens") or 0)


def report_cache_stats() -> None:
    """Print the one-line usage footer for the calls this run made since
    its last footer, if there were any.

    A harvest pass and a web-UI op print theirs as they end; every run
    prints what is left as it ends (the claude_usage_summary setting), so
    a CLI one-shot prints its total once.

    `hit` is the share of the CACHEABLE prefix served from cache — 0% across
    a whole run with a large system prompt means something is invalidating
    the prefix (a timestamp in it, a changed model, a changed profile
    mid-run).
    """
    calls = _CALLS()
    s = {k: v - calls.reported[k] for k, v in calls.usage.items()}
    calls.reported = dict(calls.usage)
    if s["calls"] <= 0:
        return
    cached = s["cache_read"] + s["cache_write"]
    hit = (100.0 * s["cache_read"] / cached) if cached else 0.0
    total_in = cached + s["uncached_input"]
    print(f"  [claude] {s['calls']} call(s) | input {total_in:,} tok "
          f"(cache read {s['cache_read']:,}, wrote {s['cache_write']:,}, "
          f"uncached {s['uncached_input']:,}; {hit:.0f}% of cacheable prefix hit) "
          f"| output {s['output']:,} tok")


# Unrecoverable-API-error circuit breaker. Some failures can never succeed on
# retry within the same run — an exhausted credit balance (400), a bad or
# revoked API key (401/403). Without a breaker each job's call fails
# independently: the 2026-08-31 rescore burned 973 consecutive "credit balance
# is too low" 400s over five minutes before finishing. Once tripped, every
# later call in the run returns None immediately without touching the API;
# the next run (a web UI op, a harvest pass) asks again, so a topped-up
# balance takes effect without a restart, and a still-dead API fails once
# and explains itself (2026-09-09: a crawl tripped it at 18:22, and the
# verify runs at 18:29 and 19:24 skipped every call without saying why).


def _trip_fatal(msg: str) -> None:
    calls = _CALLS()
    if calls.fatal is None:
        calls.fatal = msg
        print(f"  [!] Claude API disabled for the rest of this run "
              f"(unrecoverable): {msg}")


def api_disabled() -> str | None:
    """The breaker's message while an unrecoverable API error has disabled
    Claude calls for this run, else None. Long loops (the deep-verify pass)
    check it to stop with one line instead of a per-row 'unverified' for
    work the API can no longer do."""
    return _CALLS().fatal


def have_api_key() -> bool:
    """Whether an API key is configured at all. call_claude_json returns
    None without asking when it is not, exactly as it does for a refusal, so
    a caller that has to tell "the model said nothing" apart from "we
    never asked" (src.claude.fit.score_resume_fit) asks here first.

    >>> isinstance(have_api_key(), bool)
    True
    """
    return config.ANTHROPIC_API_KEY != "YOUR_ANTHROPIC_API_KEY_HERE"


async def call_claude_json[R: Reply](system_prompt: str, user_content: str,
                                     max_tokens: int = 1000, model: str | None = None,
                                     thinking: bool = False, cache: bool = True, *,
                                     reply: type[R]) -> R | None:
    """POST to /v1/messages and return Claude's answer as a `reply` (a Reply
    subclass) instance, held to its schema (see build_payload).

    None is the one "no answer": no key, a tripped breaker, an HTTP error,
    no text, non-JSON, or an answer that fails `reply`'s validation, which
    prints one line naming the failing fields.

    `model` overrides config.CLAUDE_MODEL for this call (the deep-verify
    pass runs a stronger model than the screen). `thinking=True` leaves the
    model's default adaptive thinking on (5-family models) — pair it with a
    max_tokens large enough for thinking + the JSON; the default False
    turns thinking off (to effort low on models that always think) so it
    does not eat a small structured call's budget.
    `cache=False` opts this call out of the system-prompt cache breakpoint
    (see the prompt-caching block above); the default is on everywhere.

    Every call that reaches the API logs a "claude" DEBUG record with its
    final HTTP status (or why it failed) and the elapsed seconds, retries
    included.

    Notes:
        The POST is plain (net.http's polite=False): no robots.txt, no
        Crawl-delay and no crawl trace, since the endpoint is not a crawl
        target, and a bare requests session's headers. Without this
        record API latency never reached the session log."""
    if not have_api_key():
        print("  [!] Set the ANTHROPIC_API_KEY environment variable.")
        return None
    if (fatal := api_disabled()) is not None:
        _log.debug("claude call skipped (breaker tripped): %s", fatal)
        return None
    use_model = model or config.CLAUDE_MODEL
    payload = build_payload(system_prompt, user_content, max_tokens,
                            use_model, thinking, cache, reply=reply)
    lead = await _claim_prefix(use_model, system_prompt) \
        if (cache and config.SETTINGS.claude_prompt_cache and system_prompt) \
        else None
    t0, delays = time.monotonic(), config.CLAUDE_RETRY_DELAYS_S
    try:
        for attempt in range(len(delays) + 1):
            r = await http.send(
                "POST", "https://api.anthropic.com/v1/messages",
                polite=False,
                headers={
                    "x-api-key":         config.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json=payload,
                timeout=config.CLAUDE_TIMEOUT,
            )
            # Transient statuses worth one short retry ladder (529 = overloaded_error).
            if r.status_code in (429, 500, 502, 503, 529) and attempt < len(delays):
                try:
                    delay = float(r.headers.get("retry-after", ""))
                except ValueError:
                    delay = delays[attempt]
                _log.debug("claude %s -> retrying in %.0fs (attempt %d)",
                           r.status_code, delay, attempt + 1)
                await asyncio.sleep(min(delay, 60.0))
                continue
            break
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        _record_usage(usage)
        _log.debug("%s: %s in / %s out tokens (cache read: %s) "
                   "| HTTP %s in %.2fs",
                   use_model, usage.get("input_tokens"),
                   usage.get("output_tokens"),
                   usage.get("cache_read_input_tokens", 0),
                   r.status_code, time.monotonic() - t0)
        blocks = data.get("content", [])
        answer = next((b.get("input") for b in blocks
                       if b.get("type") == "tool_use"), None)
        if answer is None:
            text = next((b["text"] for b in blocks
                         if b.get("type") == "text"), "").strip()
            if not text:
                # Two "no answer" shapes that aren't parse errors: adaptive
                # thinking exhausting max_tokens before any text lands
                # (stop_reason max_tokens: raise the caller's budget), and
                # a safety refusal (stop_reason refusal: no text block).
                print(f"  [!] Claude returned no text "
                      f"(stop_reason={data.get('stop_reason')})")
                return None
            # strict=False: the model occasionally emitted a literal
            # newline/tab INSIDE a JSON string value, which strict
            # json.loads rejects as "Invalid control character"; it cost a
            # company its mission score in the 2026-08-28 discover-local
            # session.
            answer = json.loads(text, strict=False)
        return reply.model_validate(answer)
    except ValidationError as e:
        errs = e.errors()
        where = "; ".join(f"{'.'.join(map(str, x['loc'])) or '(root)'}: "
                          f"{x['msg']}" for x in errs[:3])
        more = f" (+{len(errs) - 3} more)" if len(errs) > 3 else ""
        _log.debug("claude call failed: invalid %s in %.2fs",
                   reply.__name__, time.monotonic() - t0)
        print(f"  [!] Claude reply is not a valid {reply.__name__}: "
              f"{where}{more}")
        return None
    except http.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        body = getattr(e.response, "text", "")[:300]
        _log.debug("claude call failed: HTTP %s in %.2fs",
                   status, time.monotonic() - t0)
        if status in (401, 403) or (status == 400
                                    and "credit balance" in body.lower()):
            _trip_fatal(f"HTTP {status}: {body!r}")
        else:
            print(f"  [!] Claude API error: {e}  body={body!r}")
        return None
    except json.JSONDecodeError as e:
        _log.debug("claude call failed: non-JSON response in %.2fs",
                   time.monotonic() - t0)
        print(f"  [!] Claude returned non-JSON: {e}")
        return None
    except Exception as e:
        _log.debug("claude call failed: %s in %.2fs",
                   type(e).__name__, time.monotonic() - t0)
        print(f"  [!] Claude call failed: {e}")
        return None
    finally:
        # Release any calls waiting on this prefix — including on failure,
        # so one bad request can't stall a scoring fan-out for the gate wait.
        if lead is not None:
            lead.set()


async def expand_search(term: str) -> ExpandReply | None:
    system = f"""You are a job-search strategist for this candidate:

{_CANDIDATE}
{_AVOID}

Given a job title, skill, or concept, return ONLY a JSON object with exactly three keys:
- "titles": array of up to 12 alternative job-title strings to search for, matched to the candidate's reachable level.
- "keywords": array of up to 12 technical keywords/skills/domain terms that surface more relevant listings.
- "sectors": array of up to 12 company types, industry verticals, or named employers/labs where these roles exist."""
    return await call_claude_json(system, term, reply=ExpandReply)


async def expand_location(term: str) -> LocationReply | None:
    system = """You are a geographic search strategist. Given a location term (a city, region, country, or qualifier like "remote"), return ONLY a JSON object with exactly two keys:
- "include": array of up to 15 related location strings that should ALSO match when filtering jobs for this area. Examples: for "North Carolina", include "NC", "Durham", "Raleigh", "Chapel Hill", "Research Triangle", "RTP". For "remote", include "work from home", "wfh", "fully remote", "distributed", "anywhere".
- "exclude": array of up to 8 location strings that should be explicitly excluded when someone specifies this search. Examples: for "us only", include common offshore locations the user likely wants to filter out.
Use lowercase unless the token is normally capitalized (country codes etc)."""
    return await call_claude_json(system, term, reply=LocationReply)


class MissionReply(Reply):
    mission: Annotated[str, OneOf(_MISSION_TIERS, loose=True)]
    score: Unit
    reason: str

    @property
    def tier(self) -> str | None:
        """`mission` when the profile names that tier, else None."""
        return self.mission if self.mission in _MISSION_TIERS else None


# Résumé-fit scoring lives in src/claude/fit.py (multi-axis rubric + gates);
# callers import score_resume_fit from there. The old single-scalar prompt
# and its _STRENGTHS / _FIT_CAPS blocks were retired with it.


async def score_company_mission(name: str,
                                context: str = "") -> tuple[str | None, float | None, str]:
    """Return (mission_tier|None, score|None, reason) for an employer."""
    # Deterministic bullseye anchor (profile [mission].bullseye_regex), checked
    # BEFORE the LLM: a company whose NAME is the candidate's exact target is
    # pinned to 1.0 in the bullseye tier with no API call. The name is the whole
    # signal here, and the mission model often can't see a client-rendered
    # careers page anyway (this is how Science.xyz got mis-scored to 0.10).
    # Match the NAME only, never the reason text, where the model's negations
    # live ("no neurotech focus") and a substring match would invert the result.
    bullseye = config.MISSION_BULLSEYE_REGEX
    if bullseye and re.search(bullseye, name.lower(), re.I):
        return config.MISSION_BULLSEYE_TIER or None, 1.0, "bullseye: named target"
    tiers = "\n".join(f'    "{t["name"]}" — {t["desc"]}' for t in config.MISSION_TIERS)
    system = f"""You score how well an EMPLOYER matches a specific candidate's ideal target, from 0.0 to 1.0. Given a company name + sample postings, judge the COMPANY (not one role).

{_CANDIDATE}

Return ONLY a JSON object with exactly:
- "mission": one of
{tiers}
- "score": 0.0-1.0 alignment with the candidate's target — pick within the band for the tier you chose:
{_tier_bands()}
- "reason": one short phrase (<= 12 words)."""
    user = f"COMPANY: {name}\n\nSAMPLE POSTINGS / CONTEXT:\n{(context or '(none)')[:1500]}"
    r = await call_claude_json(system, user, max_tokens=120, reply=MissionReply)
    if r is None:
        return None, None, ""
    return r.tier, r.score, r.reason


class BoardOwnerReply(Reply):
    same_employer: StrictBool
    reason: str


#: (company, board) -> the task of its paid call while it runs, then for
#: the process a done future of its True/False (see board_is_own). Not run
#: state (src/runstate.py): the next web UI op or harvest pass reuses it.
_BOARD_OWNER_CACHE: dict[tuple[str, str], asyncio.Future[bool | None]] = {}


async def board_is_own(company: str, board: str, site: str = "",
                       titles: Iterable[str] = ()) -> bool | None:
    """True/False: is `board` (a Workday tenant, or "<ats>:<slug>")
    `company`'s own hiring board? None when the API is unavailable or the
    reply is malformed — callers keep the hit on None (offline behavior
    unchanged) and only reject on a clear False. Verdicts are cached per
    (company, board) for the process, one call per pair however many ask;
    a None or an error is asked again by the next asker.

    The call is a task of its own that every asker awaits shielded: an
    asker cancelled mid-call (a JS scrape's budget) leaves it to finish,
    and its verdict is kept for the next one, the run's end waiting for it
    (see _Calls).

    Notes:
        Consulted only for collision-prone resolutions, a board on a
        platform whose spec sets `discovery.shared` sharing no token with the
        name (src.discovery.resolve.identity.foreign_board), so this costs a
        call on the rare suspect, not per resolve. The
        asymmetric default matters: a wrong "keep" mislabels one company
        until a human looks, a wrong "reject" silently loses a real board
        forever. `titles` (sample postings from the board) is the decisive
        evidence for slug collisions.
    """
    async def ask() -> bool | None:
        system = (
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
        user = f"COMPANY: {company}\nBOARD: {board}"
        if site:
            user += f"\nBOARD DISPLAY NAME / SITE: {site}"
        if titles:
            user += "\nSAMPLE JOB TITLES: " + " | ".join(
                t for t in list(titles)[:8] if t)
        r = await call_claude_json(system, user, max_tokens=150, reply=BoardOwnerReply)
        return None if r is None else r.same_employer
    key = (str(company).lower(), str(board).lower())
    task = _BOARD_OWNER_CACHE.get(key)
    if task is None:
        task = _BOARD_OWNER_CACHE[key] = asyncio.create_task(ask())
        asking = _CALLS().asking
        asking.add(task)
        task.add_done_callback(asking.discard)

        def settled(t: asyncio.Future[bool | None]) -> None:
            # Runs before any asker resumes; exception() marks an error seen.
            if _BOARD_OWNER_CACHE.get(key) is not t:
                return
            if t.cancelled() or t.exception() is not None or t.result() is None:
                del _BOARD_OWNER_CACHE[key]
            else:       # the verdict alone: a kept task pins its run's state
                _BOARD_OWNER_CACHE[key] = kept = t.get_loop().create_future()
                kept.set_result(t.result())
        task.add_done_callback(settled)
    return await asyncio.shield(task)
