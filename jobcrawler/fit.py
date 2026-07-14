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

Wiring (three small edits, all backward-compatible):
  1. claude.py:  `from jobcrawler import fit` and make `score_resume_fit`
     delegate:  `return fit.score_resume_fit(title, description).as_legacy()`
  2. config.py:  load an optional `[fit]` profile block (weights / gate
     penalties / taxonomy overrides); omit it and the defaults below apply.
  3. store.py :  add `"fit_axes": "TEXT"` to _MIGRATIONS["jobs"] and persist
     FitResult.axes_json to it. The scalar still lands in resume_fit_score, so
     ranked_jobs() and combined_score() keep working untouched.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

try:
    import config
    from jobcrawler.claude import call_claude_json
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
}  # calibrated on the anchor set below: MAE 0.04, rank-agreement 0.88

# A failed gate multiplies the base score. We take the WORST (min) gate rather
# than the product, so a doubly-disqualified role sinks once, hard, instead of
# to a pathological ~0 that hides ordering among the disqualified.
DEFAULT_GATE_PENALTY = {
    "geo":      0.20,    # not remote and not in-region (hard constraint)
    "embedded": 0.40,    # core work is firmware/PCB/analog/RTOS (avoid-list)
    "level":    0.35,    # below-bar: ops/coordination/data-entry/analyst-only
    "phd":      0.45,    # hard PhD requirement with no equivalent-experience path
}

GATES = tuple(DEFAULT_GATE_PENALTY)

# Domain ladder + stack vocabulary default to a neural/biosignal candidate.
# Override any of these from profile.toml [fit] (loaded via config.FIT_*); the
# prompt renders whatever is loaded, so the taxonomy is data, not hard-code.
DEFAULT_DOMAIN_LADDER = [
    (1.00, "iEEG, ECoG, intracranial EEG, EEG, electrophysiology, high-gamma, neural decoding"),
    (0.80, "LFP, spikes, single-unit, MEG, other neural, BCI, neurotech"),
    (0.65, "EMG, ECG, EOG, PPG, other physiological biosignal, wearable sensors"),
    (0.60, "fMRI, MRI, DTI, medical imaging, neuroimaging"),
    (0.45, "clinical/EHR data, genomics, digital health, medical devices"),
    (0.15, "no health, bio, or neural component"),
]
DEFAULT_STACK_CORE = ("Python, PyTorch, NumPy/SciPy, CUDA/CuPy, C/C++, MATLAB, SQL/SQLite, "
                      "Bash, Git, CI/CD, AWS, SLURM/HPC, signal processing, BIDS/NWB, scikit-learn, MNE")
DEFAULT_STACK_ANTI = ("Snowflake, dbt, Spark, Airflow, data-warehouse modeling, Kubernetes, "
                      "Terraform, Kafka, Go, Rust, RTOS/firmware, PCB/analog, Salesforce, Power BI")
DEFAULT_REGION = "remote, or Durham / Raleigh / Chapel Hill / Research Triangle (RTP), North Carolina"


def _cfg(name, default):
    return (getattr(config, name, None) or default) if config else default


def _domain_ladder_text():
    ladder = _cfg("FIT_DOMAIN_LADDER", None)
    rungs = ([(r.get("score"), ", ".join(r.get("terms", []))) for r in ladder]
             if ladder else DEFAULT_DOMAIN_LADDER)
    return "; ".join(f"{txt} ~{score:.2f}" for score, txt in rungs)


@dataclass
class FitResult:
    """One role's fit: the scalar, the axis vector, the tripped gates, why."""
    score: float
    axes: dict = field(default_factory=dict)
    gates: list = field(default_factory=list)   # names of FAILED gates
    reason: str = ""

    @property
    def axes_json(self) -> str:
        return json.dumps({"axes": self.axes, "gates": self.gates})

    def summary(self) -> str:
        """Compact one-liner that carries the vector into fit_reason/reports."""
        a = " ".join(f"{k[:3]}{self.axes.get(k, 0):.2f}" for k in AXES)
        g = f" gate:{'+'.join(self.gates)}" if self.gates else ""
        return f"[{a}{g}] {self.reason}".strip()

    def as_legacy(self):
        """`(fit, reason)` tuple so existing callers don't change."""
        return self.score, self.summary()


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
        return f"{summary}\nStrengths (priority order):\n{strengths}\n{avoid}".strip()
    return "A technical candidate. (No profile loaded; judge on general merit.)"


def build_system_prompt() -> str:
    return f"""You are a hiring screener scoring how well ONE job fits this candidate:

{_profile_block()}

Judge from the job's ACTUAL responsibilities in the description, not its title.
Score each of four axes from 0.00 to 1.00, independently:

- "domain": overlap of the role's subject matter with the candidate's domain,
  graded on this ladder with partial credit for neighbours:
  {_domain_ladder_text()}.
- "function": how well the role's DISCIPLINE matches the candidate. Research /
  ML-modelling / research-software / scientific-pipeline engineering score
  high; analytics-warehouse data engineering, embedded/firmware, generic
  backend/SRE, data-ops/analyst score low EVEN IF the title overlaps.
- "stack": overlap of the tools the JD actually requires with the candidate's
  stack ({_cfg("FIT_STACK_CORE", DEFAULT_STACK_CORE)}). Weight load-bearing
  requirements heavily. If the JD centres on tools OUTSIDE that stack
  ({_cfg("FIT_STACK_ANTI", DEFAULT_STACK_ANTI)}), score low no matter what the
  title says.
- "seniority": does the candidate clear the level without being wildly over- or
  under-qualified. Principal/Staff for a mid-level candidate scores low.

Then set any GATES that apply (these are disqualifiers, not deductions):
- "geo": true if the role is neither remote nor in the candidate's region
  ({_cfg("FIT_REGION", DEFAULT_REGION)}).
- "embedded": true if the core work is firmware, PCB, analog, or RTOS.
- "level": true if the role is below the candidate's technical bar (SOP
  execution, coordination, monitoring, manual data entry, analyst-only).
- "phd": true only if a PhD is a HARD requirement with no equivalent-experience path.

Return ONLY a JSON object with exactly:
- "domain", "function", "stack", "seniority": numbers 0.00-1.00.
- "gates": array of the gate names that are TRUE (empty array if none).
- "reason": one short phrase (<= 14 words) naming the deciding factor.
Return ONLY valid JSON. No markdown, no preamble."""


def score_resume_fit(title: str, description: str = "", *, max_tokens=220) -> FitResult:
    """Score one posting. Falls back to a neutral empty result if the API is
    unavailable, so callers degrade instead of crashing (matches the old fn)."""
    if call_claude_json is None:
        return FitResult(score=None, reason="scorer unavailable")
    desc = (description or "")[:2500]
    user = f"JOB TITLE: {title}\nJOB DESCRIPTION:\n{desc or '(no description)'}"
    r = call_claude_json(build_system_prompt(), user, max_tokens=max_tokens)
    if not r or "function" not in r:
        return FitResult(score=None, reason="unscored")
    axes = {a: _clamp(r.get(a)) for a in AXES}
    gates = [g for g in r.get("gates", []) if g in GATES]
    weights = getattr(config, "FIT_WEIGHTS", None)
    penalties = getattr(config, "FIT_GATE_PENALTY", None)
    score = combine(axes, gates, weights, penalties)
    # Missing-JD guard, mirroring the old fit_caps: no body -> cap at 0.45.
    if not desc:
        score = min(score, 0.45)
    return FitResult(score=score, axes=axes, gates=gates,
                     reason=str(r.get("reason", "")).strip())


def _clamp(x):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
#  Calibration harness. Anchors are the roles we hand-scored against the full  #
#  portfolio (JD-grounded where possible). Each carries the axis vector that   #
#  reasoning implied, the gates it trips, and the holistic hand score. Run     #
#  `python -m jobcrawler.fit` to see predicted-vs-hand and tune the weights.   #
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
