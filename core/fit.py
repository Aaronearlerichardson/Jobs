"""
Multi-axis résumé-fit scorer.

Replaces the single-scalar `score_resume_fit` with a rubric that scores a role
on a few ORTHOGONAL axes and then GATES on disqualifiers. This fixes the two
failure modes of keyword/one-scalar scoring:

  * too specific  -> every non-domain role looks identical.  Solved: the
    `function` and `stack` axes still separate a warehouse Data Engineer from
    an embedded DSP Engineer even when both score ~0 on `domain`.
  * too general   -> one kind of experience is mistaken for another.  Solved:
    `stack` is matched as an explicit set, so the *word* "data engineer" can't
    launder a Snowflake/dbt role into scientific-pipeline experience.

Division of labour: the LLM judges each axis and flags gates (what it is good
at); Python does the arithmetic (transparent, tunable, calibratable). The
combiner is a weighted geometric mean -- the same imbalance-punishing shape
store.combined_score() already uses -- times the worst gate multiplier.

Wired into:
  - claude.py:  `score_resume_fit(resume, title, desc)` delegates here and
    returns the FitResult (resume is ignored; the rubric scores the profile).
  - config.py:  loads the optional `[fit]` profile block (weights / gate
    penalties / domain ladder / stack / region); omit it and the defaults
    below apply.
  - store.py :  jobs table carries one column per axis (fit_domain/function/
    stack/seniority) plus fit_gates; FitResult.as_columns() produces them and
    store.update_job_scores() writes them. resume_fit_score stays the combined
    scalar, so ranked_jobs()/combined_score() keep working untouched.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

try:
    import config
    from core.claude import call_claude_json
except Exception:                      # importable standalone for calibration
    config = None
    call_claude_json = None


# --------------------------------------------------------------------------- #
#  Rubric taxonomy (defaults; override any of these from profile.toml [fit]).  #
#  Kept to FOUR axes on purpose: averaging more axes regresses every role to   #
#  the mean and quietly brings back the "everything looks similar" problem.    #
#  Let the gates, not more axes, create the spread.                            #
# --------------------------------------------------------------------------- #

AXES = ("domain", "function", "stack", "seniority")

DEFAULT_WEIGHTS = {
    "domain":    0.25,   # modality ladder: iEEG/EEG > neural > biosignal > imaging > health > none
    "function":  0.34,   # role discipline from the JD BODY, not the title
    "stack":     0.33,   # explicit tool overlap, load-bearing tools weighted
    "seniority": 0.08,   # clears the bar without being wildly over/under
}  # calibrated on the 17-role anchor set below: MAE 0.036, rank-agreement 0.91

# A failed gate multiplies the base score. We take the WORST (min) gate rather
# than the product, so a doubly-disqualified role sinks once, hard, instead of
# to a pathological ~0 that hides ordering among the disqualified.
DEFAULT_GATE_PENALTY = {
    "geo":      0.20,    # not remote and not in-region (hard constraint)
    "embedded": 0.40,    # core work is firmware/PCB/analog/RTOS (avoid-list)
    "level":    0.35,    # below-bar: ops/coordination/data-entry/analyst-only
    "phd":      0.45,    # hard PhD requirement with no equivalent-experience path
    # Wrong SEAT, not wrong level: the role's core is people/program/product
    # management (directing engineers/contractors, PRDs, roadmaps, governance)
    # and the candidate is an IC. Harsher than phd — equivalent experience can
    # argue past a PhD line; it cannot conjure a management track record.
    "management": 0.35,
    # Role demands an ACTIVE/current security clearance the candidate doesn't
    # hold (Vadum-class). Same weight as phd: sometimes arguable past (an
    # employer can sponsor a clearable citizen), unlike a management track
    # record. Citizenship-only or "able to obtain" requirements do NOT trip
    # this — the candidate is a US citizen. Detected by the LLM AND a
    # deterministic regex backstop (_CLEARANCE_RE) on the posting text.
    "clearance": 0.45,
}

GATES = tuple(DEFAULT_GATE_PENALTY)


def _effective_penalties():
    """DEFAULT_GATE_PENALTY overlaid with any profile.toml [fit] gate_penalty
    entries. MERGED, not replaced: a profile written before a gate existed
    (e.g. one listing only geo/embedded/level/phd) must not silently disable
    the newer gates by omission — penalties.get(gate, 1.0) would neutralize
    them."""
    override = (getattr(config, "FIT_GATE_PENALTY", None) or {}) if config else {}
    return {**DEFAULT_GATE_PENALTY, **override}

# The domain ladder, stack vocabulary, and region are the parts of the rubric
# that are ABOUT YOU, so there is no honest hard-coded default for them. Set
# them explicitly in profile.toml [fit] (loaded via config.FIT_*) for a tuned
# search; leave [fit] out and they are DERIVED from the profile you already
# wrote — your keyword tiers are a serviceable domain ladder, your `skill`
# tier is a serviceable stack, and [locality] is your region. The prompt
# renders whatever is loaded, so the taxonomy is data either way.
#
# The last-resort literals below only apply to an empty profile.
FALLBACK_DOMAIN_LADDER = [
    (1.00, "the exact subject matter you work on"),
    (0.15, "an unrelated field"),
]
FALLBACK_STACK_CORE = "the tools named in your résumé"
FALLBACK_REGION = "remote"


def _cfg(name, default):
    return (getattr(config, name, None) or default) if config else default


def _derived_domain_ladder():
    """A domain ladder built from the profile's keyword tiers.

    `core` terms are what you actually want (top rung), `domain` terms are the
    adjacent fields you'd accept (middle), and everything else is the floor.
    Crude next to a hand-tuned ladder, but it is genuinely YOURS on day one
    instead of being someone else's field."""
    core = _cfg("CORE_KEYWORDS", [])
    domain = _cfg("DOMAIN_KEYWORDS", [])
    rungs = []
    if core:
        rungs.append((1.00, ", ".join(core[:12])))
    if domain:
        rungs.append((0.60, ", ".join(domain[:12])))
    if not rungs:
        return FALLBACK_DOMAIN_LADDER
    rungs.append((0.15, "no connection to any of the above"))
    return rungs


def _derived_region():
    """The region string for the geo gate, from [locality]."""
    name = _cfg("LOCALITY_NAME", "")
    places = list(_cfg("LOCALITY_SUBSTRINGS", []))[:8]
    if not name and not places:
        return FALLBACK_REGION
    where = name or ", ".join(places)
    if places and name:
        where = f"{name} ({', '.join(places)})"
    return f"remote, or {where}"


def _domain_ladder_text():
    ladder = _cfg("FIT_DOMAIN_LADDER", None)
    rungs = ([(r.get("score"), ", ".join(r.get("terms", []))) for r in ladder]
             if ladder else _derived_domain_ladder())
    return "; ".join(f"{txt} ~{score:.2f}" for score, txt in rungs)


def _stack_core_text():
    """The candidate's tools: [fit].stack_core, else the profile's `skill`
    keyword tier — which is already a list of the tools they work with."""
    return _cfg("FIT_STACK_CORE", None) or (
        ", ".join(_cfg("SKILL_KEYWORDS", [])) or FALLBACK_STACK_CORE)


def _anti_stack_clause():
    """The 'tools that disqualify' half of the stack axis — only rendered when
    the profile names some. There is no way to derive an anti-stack (the tools
    you DON'T want are not implied by the ones you do), and inventing one
    would silently penalise roles the user never objected to."""
    anti = _cfg("FIT_STACK_ANTI", None)
    if not anti:
        return ""
    return (f" If the JD centres on tools OUTSIDE that stack ({anti}), score"
            " low no matter what the title says; if any of those is a stated"
            " REQUIREMENT (required/must-have, not merely preferred), stack is"
            " at most 0.20.")


@dataclass
class FitResult:
    """One role's fit: the scalar, the axis vector, the tripped gates, why."""
    score: float
    axes: dict = field(default_factory=dict)
    gates: list = field(default_factory=list)   # names of FAILED gates
    reason: str = ""

    def summary(self) -> str:
        """Compact one-liner that carries the vector into fit_reason/reports.
        For an unscored result (no axes) it is just the plain reason, so a
        None-scored row doesn't get a misleading [dom0.00 ...] tag."""
        if not self.axes:
            return self.reason
        a = " ".join(f"{k[:3]}{self.axes.get(k, 0):.2f}" for k in AXES)
        g = f" gate:{'+'.join(self.gates)}" if self.gates else ""
        return f"[{a}{g}] {self.reason}".strip()

    def as_legacy(self):
        """`(fit, reason)` tuple for any caller that only wants the scalar."""
        return self.score, self.summary()

    def as_columns(self) -> dict:
        """DB-ready fields: the scalar, the reason tag, the tripped gates, and
        one column per axis. Keys match the jobs-table columns added in
        store.py. Axes are None on an unscored result, so those columns clear."""
        cols = {"resume_fit_score": self.score,
                "fit_reason": self.summary(),
                "fit_gates": ",".join(self.gates) or None}
        cols.update({f"fit_{a}": self.axes.get(a) for a in AXES})
        return cols


# --------------------------------------------------------------------------- #
#  Pure combiner -- no API, no I/O; this is the unit-testable / calibratable   #
#  core. Weighted geometric mean of the axes, times the worst gate penalty.    #
# --------------------------------------------------------------------------- #

def combine(axes: dict, failed_gates=(), weights=None, penalties=None) -> float:
    weights = weights or DEFAULT_WEIGHTS
    penalties = penalties or DEFAULT_GATE_PENALTY
    eps = 1e-6
    wsum = sum(weights[a] for a in AXES) or 1.0
    log = sum(weights[a] * math.log(max(eps, float(axes.get(a, 0.0)))) for a in AXES)
    base = math.exp(log / wsum)
    mult = min([penalties.get(g, 1.0) for g in failed_gates] + [1.0])
    return round(base * mult, 2)


# --------------------------------------------------------------------------- #
#  Prompt: the LLM returns per-axis subscores + tripped gates + one reason.    #
#  It does NOT return the final number -- Python owns that so it stays tunable #
#  and honest. Candidate profile is injected from config (whoever is loaded).  #
# --------------------------------------------------------------------------- #

def _profile_block():
    if config and getattr(config, "CANDIDATE_STRENGTHS", None):
        strengths = "\n".join(f"  {i}. {s}" for i, s in enumerate(config.CANDIDATE_STRENGTHS, 1))
        summary = getattr(config, "CANDIDATE_SUMMARY", "") or ""
        avoid = getattr(config, "CANDIDATE_AVOID", "") or ""
        # profile.toml [candidate] fit_caps. The model never returns the final
        # scalar (Python owns the combine), so a cap is enforced by bounding
        # the axis it targets: when one applies, function must sit at or below
        # the cap value. Went unread from the old scorer's retirement until
        # 2026-08 — the production-quality-ops cap is the census's #1 screen.
        caps = (getattr(config, "CANDIDATE_FIT_CAPS", None) or [])
        caps_block = ("\nHard caps — when one applies, score the FUNCTION axis "
                      "at or below the cap value:\n"
                      + "\n".join(f"  - {c}" for c in caps)) if caps else ""
        return f"{summary}\nStrengths (priority order):\n{strengths}\n{avoid}{caps_block}".strip()
    return "A technical candidate. (No profile loaded; judge on general merit.)"


def disposition_examples_block(conn, limit=3):
    """Few-shot calibration from the candidate's OWN recorded decisions
    (crawler.py --mark): up to `limit` applied/interviewing postings as
    positive examples and `limit` dismissed ones as negatives, newest
    first. Returns '' when there are none. A --why note rides along
    verbatim, which is exactly how 'wrong archetype — TPM seat' style
    lessons reach the scorer."""
    pos = conn.execute(
        "SELECT title, company_name FROM jobs "
        "WHERE disposition IN ('applied','interviewing') "
        "ORDER BY disposition_at DESC LIMIT ?", (limit,)).fetchall()
    neg = conn.execute(
        "SELECT title, company_name, disposition_note FROM jobs "
        "WHERE disposition = 'dismissed' "
        "ORDER BY disposition_at DESC LIMIT ?", (limit,)).fetchall()
    if not pos and not neg:
        return ""
    lines = ["", "CALIBRATION — this candidate's own recorded decisions on real postings:"]
    for r in pos:
        lines.append(f'- PURSUED: "{(r["title"] or "")[:70]}" at {r["company_name"]}')
    for r in neg:
        note = (r["disposition_note"] or "").strip()
        tail = f' — their reason: "{note[:90]}"' if note else ""
        lines.append(f'- DISMISSED: "{(r["title"] or "")[:70]}" at {r["company_name"]}{tail}')
    lines.append("Treat these as ground truth about what this candidate wants; "
                 "score similar roles consistently with them.")
    return "\n".join(lines)


_DISPO_BLOCK_CACHE = None


def _disposition_block() -> str:
    """disposition_examples_block over the default store, computed once per
    process (a crawl scores hundreds of jobs; dispositions don't change
    mid-run). profile.toml [fit] disposition_examples sets the count
    (0 disables); silently empty when the store is unavailable."""
    global _DISPO_BLOCK_CACHE
    if _DISPO_BLOCK_CACHE is None:
        n = getattr(config, "FIT_DISPOSITION_EXAMPLES", None) if config else 0
        n = 3 if n is None else int(n)   # NOT _cfg(): 0 must mean "off"
        block = ""
        if n > 0:
            try:
                from core import store
                conn = store.connect()
                block = disposition_examples_block(conn, n)
                conn.close()
            except Exception:
                block = ""
        _DISPO_BLOCK_CACHE = block
    return _DISPO_BLOCK_CACHE


def _axes_and_gates_block() -> str:
    """The axis + gate rubric shared verbatim by the first-pass screen
    (build_system_prompt) and the finalist re-screen (build_verify_prompt),
    so the two passes can never drift apart on definitions."""
    return f"""Score each of four axes from 0.00 to 1.00, independently:

- "domain": overlap of the ROLE'S OWN day-to-day subject matter with the
  candidate's domain, graded on this ladder with partial credit for
  neighbours: {_domain_ladder_text()}.
  Score what the role itself works on, NOT the employer's product domain —
  an internal-tooling, IT, TPM, or ops seat at a company in the candidate's
  field is NOT a role in that field, however much the company boilerplate
  mentions the mission.
- "function": how well the role's DISCIPLINE matches the kind of work the
  candidate profile above describes. Judge from the JD BODY, not the title:
  a role whose day-to-day is a different discipline scores low EVEN IF the
  title overlaps, and one whose discipline matches scores high even if the
  title is unfamiliar. People-management, program/product-management, and
  internal enablement seats are different disciplines from hands-on work.
- "stack": overlap of the tools the JD actually requires with the candidate's
  stack ({_stack_core_text()}). Weight load-bearing requirements heavily.{_anti_stack_clause()}
- "seniority": does the candidate clear the level without being wildly over- or
  under-qualified. Principal/Staff for a mid-level candidate scores low, as do
  people-management titles (Manager/Senior Manager/Director) and roles whose
  years-of-experience requirement far exceeds the candidate's.

Then set any GATES that apply (these are disqualifiers, not deductions):
- "geo": true if the role is neither remote nor in the candidate's region
  ({_cfg("FIT_REGION", None) or _derived_region()}).
- "embedded": true if the core work is firmware, PCB, analog, or RTOS.
- "level": true if the role is below the candidate's technical bar (SOP
  execution, coordination, monitoring, manual data entry, analyst-only).
- "phd": true only if a PhD is a HARD requirement with no equivalent-experience path.
- "management": true if the role's CORE is people/program/product management —
  directing engineers or contractors, owning PRDs/roadmaps/governance,
  running programs — rather than hands-on IC engineering or science. An IC
  role with some cross-team coordination is NOT management.
- "clearance": true only if the posting TEXT states an ACTIVE or current
  security clearance (Secret/TS/SCI) is required. US citizenship,
  export-control (ITAR) eligibility, or "ability to obtain a clearance" do
  NOT trip this — the candidate is a US citizen and clearable — and a
  defense/DoD employer context alone is NEVER grounds to infer it."""


def build_system_prompt() -> str:
    return f"""You are a hiring screener scoring how well ONE job fits this candidate:

{_profile_block()}
{_disposition_block()}

Judge from the job's ACTUAL responsibilities in the description, not its title.
{_axes_and_gates_block()}

Return ONLY a JSON object with exactly:
- "domain", "function", "stack", "seniority": numbers 0.00-1.00.
- "gates": array of the gate names that are TRUE (empty array if none).
- "reason": one short phrase (<= 14 words) naming the deciding factor.
Return ONLY valid JSON. No markdown, no preamble."""


def build_verify_prompt() -> str:
    return f"""You are RE-SCREENING a finalist job with its FULL posting text for this candidate:

{_profile_block()}
{_disposition_block()}

STEP 1 — extract the posting's hard requirements. Read the WHOLE text; the
requirements/qualifications block is usually near the END, after company
boilerplate, and it outranks the marketing copy at the top:
- "years_required": minimum years of experience demanded (number, or null).
- "seat_type": the role's CORE seat, one of "ic-engineering", "ic-science",
  "management", "program-product", "sales-field", "support-ops".
- "must_haves": the 3-6 load-bearing requirements as short phrases.
- "candidate_gaps": the must_haves this candidate plainly lacks.

STEP 2 — with that extraction in mind (a candidate missing the spine of the
job cannot score well however attractive the employer's domain is):
{_axes_and_gates_block()}

Return ONLY a JSON object with exactly:
- "years_required": number or null.
- "seat_type": one of the strings above.
- "must_haves", "candidate_gaps": arrays of short strings.
- "domain", "function", "stack", "seniority": numbers 0.00-1.00, on the SAME
  scale as a first-pass screen — a role squarely in the candidate's lane
  scores HIGH on those axes; gaps belong in "candidate_gaps" and "gates",
  never as blanket axis deductions.
- "gates": ARRAY of the gate names that are TRUE (empty array if none) —
  never an object of booleans.
- "reason": one phrase, <= 14 words, naming the deciding factor.
Return ONLY valid JSON. No markdown, no preamble."""


# A body shorter than this is a stub (a stored "Posted N days ago" string, a
# dead link), not a JD. We can't score it honestly, so we return None rather
# than a title-only guess that floats to the top on a fake 0.45. Matches the
# Workday backfill's notion of "missing" so the two stay consistent.
MIN_DESC_CHARS = 200

# Deterministic backstop for the "clearance" gate: ACTIVE/current-clearance
# demands are formulaic enough to regex, and a missed gate means a wasted
# application at a role the candidate cannot hold. Deliberately does NOT
# match "eligible for" / "ability to obtain" a clearance — the candidate is
# a clearable US citizen, so only holding-one-today requirements gate.
# verbs/qualifiers come from config.FIT_CLEARANCE_VERBS/_QUALIFIERS
# (profile.toml [fit] clearance_verbs / clearance_qualifiers), falling back
# to these defaults when unconfigured.
_DEFAULT_CLEARANCE_VERBS = ("active", "current")
_DEFAULT_CLEARANCE_QUALIFIERS = (
    "us", "u\\.s\\.", "government", "dod", "top[-\\s]?secret", "ts/?\\s?sci", "secret",
)


def _clearance_regex():
    cfg_verbs = getattr(config, "FIT_CLEARANCE_VERBS", None)
    cfg_quals = getattr(config, "FIT_CLEARANCE_QUALIFIERS", None)
    # Config values are plain words (escaped here); the built-in defaults are
    # already hand-tuned regex fragments (character classes for spacing
    # variants like "top secret" / "top-secret"), used verbatim.
    verbs = [re.escape(v) for v in cfg_verbs] if cfg_verbs else list(_DEFAULT_CLEARANCE_VERBS)
    quals = [re.escape(q).replace(r"\ ", r"[-\s]") for q in cfg_quals] if cfg_quals \
        else list(_DEFAULT_CLEARANCE_QUALIFIERS)
    return re.compile(
        rf"\b(?:{'|'.join(verbs)})\s+"
        rf"(?:(?:{'|'.join(quals)})\s+)*"
        r"(?:security\s+)?clearance", re.I)


_CLEARANCE_RE = _clearance_regex()


def _clearance_required(text):
    return bool(_CLEARANCE_RE.search(text or ""))

# How much of the tail survives clipping. Corporate JDs put the
# requirements/qualifications block LAST, after pages of mission boilerplate
# — a plain head-truncation is what let a TPM posting score 0.69 on its EEG
# preamble while "8+ years program management" sat unseen past the cap.
_CLIP_TAIL_CHARS = 3000


def clip_desc(text, max_chars=None):
    """Clip a JD to the scoring budget, keeping HEAD + TAIL (never head only):
    the head carries the role summary, the tail carries the requirements
    block. Marks the elision so the model knows text was cut."""
    text = text or ""
    max_chars = max_chars or _cfg("MAX_DESC_CHARS", 12000)
    if len(text) <= max_chars:
        return text
    head = max(max_chars - _CLIP_TAIL_CHARS, max_chars // 2)
    return (text[:head] + "\n[... middle of posting elided ...]\n"
            + text[-(max_chars - head):])


def score_resume_fit(title: str, description: str = "", *, max_tokens=300) -> FitResult:
    """Score one posting. Returns a None-scored result when the API is
    unavailable OR when there is no real description to assess (callers treat a
    None score as 'don't rank this'), so unscorable rows drop out instead of
    floating at a fabricated cap."""
    if call_claude_json is None:
        return FitResult(score=None, reason="scorer unavailable")
    desc = (description or "").strip()
    if len(desc) < MIN_DESC_CHARS:
        # Visible, not silent: a job that passed discovery but arrives here
        # with an empty/stub body would otherwise sit at a NULL score with
        # no trace of why — exactly what hid the Neuralink/Paradromics
        # description-hydration bug for weeks.
        print(f"    [!] SKIP-SCORE: {len(desc)}/{MIN_DESC_CHARS} char description "
              f"for {title!r} - unscored (check the fetcher/hydration path)")
        return FitResult(score=None, reason="no description; unscored")
    desc = clip_desc(desc)
    user = f"JOB TITLE: {title}\nJOB DESCRIPTION:\n{desc}"
    r = call_claude_json(build_system_prompt(), user, max_tokens=max_tokens)
    if not r or "function" not in r:
        return FitResult(score=None, reason="unscored")
    axes = {a: _clamp(r.get(a)) for a in AXES}
    gates = _parse_gates(r.get("gates"))
    # Regex backstop on the FULL pre-clip text (clipping could elide it).
    if _clearance_required(description) and "clearance" not in gates:
        gates.append("clearance")
    weights = getattr(config, "FIT_WEIGHTS", None)
    score = combine(axes, gates, weights, _effective_penalties())
    return FitResult(score=score, axes=axes, gates=gates,
                     reason=str(r.get("reason", "")).strip())


def verify_fit(title: str, description: str = "", *, max_tokens=8000) -> FitResult:
    """Deep second pass for ranking FINALISTS: same axes/gates as the screen,
    but run on the (near-)full posting text with an explicit requirements
    extraction step first — years required, seat type, must-haves, and the
    candidate's gaps — so a role whose disqualifiers live at the bottom of a
    long JD can't survive on its opening boilerplate. The extraction outranks
    the model's own gate flags: a seat_type of management/program-product
    trips the management gate even if the model forgot to set it.

    Returns a FitResult whose reason starts with "deep:" — the marker
    verify_top() uses to skip already-verified rows — with years/seat/gaps
    folded into the reason for the digest. A None score means unverifiable
    (no API, no text): callers must keep the first-pass score."""
    if call_claude_json is None:
        return FitResult(score=None, reason="scorer unavailable")
    desc = (description or "").strip()
    if len(desc) < MIN_DESC_CHARS:
        return FitResult(score=None, reason="no description; unverified")
    # Finalists get double the normal text budget: this pass exists precisely
    # to read what the screen's clip may have elided.
    desc = clip_desc(desc, max_chars=2 * _cfg("MAX_DESC_CHARS", 12000))
    user = f"JOB TITLE: {title}\nFULL JOB POSTING:\n{desc}"
    # Finalists get the stronger verify model WITH adaptive thinking left on
    # (config.CLAUDE_VERIFY_MODEL, ~15-30 bounded calls/run) — max_tokens must
    # cover thinking + the JSON on 5-family models, hence the 3000 default.
    r = call_claude_json(build_verify_prompt(), user, max_tokens=max_tokens,
                         model=_cfg("CLAUDE_VERIFY_MODEL", None), thinking=True)
    if not r or "function" not in r:
        return FitResult(score=None, reason="unverified")
    axes = {a: _clamp(r.get(a)) for a in AXES}
    gates = _parse_gates(r.get("gates"))
    seat = str(r.get("seat_type") or "").strip().lower()
    if seat in ("management", "program-product") and "management" not in gates:
        gates.append("management")
    if _clearance_required(description) and "clearance" not in gates:
        gates.append("clearance")
    score = combine(axes, gates, getattr(config, "FIT_WEIGHTS", None),
                    _effective_penalties())
    bits = []
    yrs = r.get("years_required")
    if yrs:
        bits.append(f"{yrs}+yrs")
    if seat:
        bits.append(seat)
    gaps = [str(g).strip() for g in (r.get("candidate_gaps") or []) if str(g).strip()]
    if gaps:
        bits.append("gaps: " + "; ".join(gaps[:3]))
    reason = "deep: " + str(r.get("reason", "")).strip()
    if bits:
        reason += f" [{' | '.join(bits)}]"
    return FitResult(score=score, axes=axes, gates=gates, reason=reason)


def _clamp(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _parse_gates(raw):
    """Tripped-gate names from the model's `gates` field, defensively.

    The contract is an ARRAY of the gate names that are TRUE — but a model
    can plausibly return a {gate: bool} OBJECT instead (the rubric defines
    each gate as '- \"x\": true if ...'). Iterating that dict yields its
    KEYS, i.e. EVERY gate name, truthy or not — which is exactly how the
    first --verify-top run marked all five gates on all 30 finalists and
    crushed the whole top list to ~0.1x. Accept both shapes; on a dict,
    keep only truthy values."""
    if isinstance(raw, dict):
        return [g for g, v in raw.items() if v and g in GATES]
    if isinstance(raw, (list, tuple)):
        return [g for g in raw if g in GATES]
    return []


# --------------------------------------------------------------------------- #
#  Calibration harness. Anchors are the roles we hand-scored against the full  #
#  portfolio (JD-grounded where possible). Each carries the axis vector that   #
#  reasoning implied, the gates it trips, and the holistic hand score. Run     #
#  `python -m core.fit` to see predicted-vs-hand and tune the weights.   #
#  This tests the COMBINER offline; the LLM axis-scoring needs an API key.     #
# --------------------------------------------------------------------------- #

# name: (axes dict, failed gates, hand_score, note)
_ANCHORS = {
    "Sphere DS Biomedical Signal":  (dict(domain=.85, function=.85, stack=.60, seniority=1.0), [], .70, "remote biosignal ML, his lane"),
    "Zyphra Research Eng BCI":      (dict(domain=.95, function=.80, stack=.55, seniority=.90), [], .66, "EEG+PyTorch; gen-model/multinode gap"),
    "Bandwidth AI Eng R&D":         (dict(domain=.30, function=.78, stack=.68, seniority=1.0), [], .60, "R&D generalist; non-health domain"),
    "BD Engineer II":               (dict(domain=.45, function=.72, stack=.70, seniority=1.0), [], .55, "sci-software tooling; +referral"),
    "Pedestal Data Engineer":       (dict(domain=.55, function=.45, stack=.62, seniority=1.0), [], .50, "generic app+DB; health mission"),
    "NVIDIA SWE AI Clusters":       (dict(domain=.15, function=.48, stack=.58, seniority=1.0), [], .42, "GPU-fleet SRE, not his build"),
    "Epic ML Recommendations":      (dict(domain=.10, function=.42, stack=.48, seniority=1.0), [], .35, "recsys subfield he lacks"),
    "Google SWE Cloud Storage":     (dict(domain=.10, function=.45, stack=.40, seniority=.85), [], .28, "distributed systems/Go/K8s gap"),
    "DHHS Lead Data Engineer":      (dict(domain=.30, function=.38, stack=.25, seniority=.85), [], .22, "Salesforce/mainframe mismatch"),
    "Sonova DSP (CA, embedded)":    (dict(domain=.55, function=.42, stack=.40, seniority=1.0), ["geo", "embedded"], .10, "embedded + out-of-state -> sinks"),
    "Delsys R&D SWE (MA, embedded)":(dict(domain=.60, function=.55, stack=.55, seniority=1.0), ["geo", "embedded"], .12, "good domain but onsite+embedded"),
    "Astera Research Scientist":    (dict(domain=.80, function=.70, stack=.55, seniority=.80), ["phd"], .30, "his domain but PhD-gated"),
    # The halo-trap case that motivated the management gate: Sr Manager of
    # internal AI tooling/GCP at an EEG company. First-pass rubric scored it
    # 0.69 off the EEG preamble (truncated JD); the deep read found a TPM
    # seat needing 8+ yrs program mgmt + end-to-end GCP — wrong archetype.
    "Ceribell Sr Mgr Applied AI":   (dict(domain=.35, function=.30, stack=.35, seniority=.45), ["management"], .20, "TPM/GCP seat at an EEG company; domain halo"),
    # ---- Aug 2026 census anchors: one per rule added 2026-08-11. ----------
    # CoVar guards the clearance gate's NEGATIVE space: citizenship +
    # clearance-ELIGIBILITY requirements must NOT gate (the candidate is a
    # citizen). His DSP toolkit re-pointed at RF: function/stack high,
    # domain rock-bottom, no gates — a contender on fit; the abysmal
    # company MISSION (not this score) is what keeps it out of the ranking.
    "CoVar Signal Processing Eng":  (dict(domain=.15, function=.80, stack=.70, seniority=1.0), [], .52, "his DSP toolkit at a defense shop; eligibility-only, no gate"),
    # Vadum-class guards the clearance PENALTY as the worst (only) gate: an
    # algorithm seat demanding an ACTIVE TS/SCI he doesn't hold. (Vadum's
    # bench RF/FPGA variants trip embedded too; kept clearance-only here so
    # a penalty regression can't hide behind worst-gate selection.)
    "Vadum DSP (active TS/SCI)":    (dict(domain=.15, function=.60, stack=.55, seniority=1.0), ["clearance"], .21, "cleared-shop DSP; active clearance he lacks"),
    # Guards the production-quality fit_cap (census screen risk #1): a
    # design-assurance QE posting whose core is CAPA/complaints/post-market
    # — the cap bounds FUNCTION at/below 0.45 despite the design-controls
    # keyword overlap with his DHF/ISO 14971/510(k) background.
    "Teleflex Sr QE (CAPA-heavy)":  (dict(domain=.45, function=.40, stack=.30, seniority=.90), [], .38, "production-quality ops core; cap holds function under .45"),
    # Lane-2 CAD-automation seat (census Tier-1, scorer ~0.48): encodes the
    # CURRENT profile stance that device R&D/automation without ML is
    # mid-tier. If the strengths list is ever rewritten to elevate Python
    # CAD/design-controls pairing, RAISE this hand score to match.
    "restor3d Automation Engineer": (dict(domain=.42, function=.45, stack=.50, seniority=.78), [], .48, "Python CAD automation under 21 CFR 820; no ML/neural content"),
}


def calibrate(weights=None, penalties=None):
    print(f"{'role':32} {'pred':>5} {'hand':>5} {'delta':>6}  gates")
    print("-" * 72)
    rows = []
    for name, (axes, gates, hand, _note) in _ANCHORS.items():
        pred = combine(axes, gates, weights, penalties)
        rows.append((name, pred, hand, gates))
        print(f"{name:32} {pred:>5.2f} {hand:>5.2f} {pred-hand:>+6.2f}  {','.join(gates) or '-'}")
    mae = sum(abs(p - h) for _, p, h, _ in rows) / len(rows)
    order_pred = [n for n, *_ in sorted(rows, key=lambda x: -x[1])]
    order_hand = [n for n, *_ in sorted(_ANCHORS.items(), key=lambda kv: -kv[1][2])]
    tau = _rank_agreement(order_pred, order_hand)
    print("-" * 72)
    print(f"MAE={mae:.3f}   rank-agreement={tau:.2f}   (1.0 = identical ordering)")
    return mae, tau


def _rank_agreement(a, b):
    idx = {n: i for i, n in enumerate(b)}
    seq = [idx[n] for n in a]
    conc = disc = 0
    for i in range(len(seq)):
        for j in range(i + 1, len(seq)):
            if seq[i] < seq[j]:
                conc += 1
            else:
                disc += 1
    total = conc + disc
    return (conc - disc) / total if total else 1.0


if __name__ == "__main__":
    calibrate()
