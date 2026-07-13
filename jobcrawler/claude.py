"""Claude API wrapper + search-expansion prompts."""

import json
import re

import requests

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL


_BCI_EXPAND_SYSTEM = """You are a job search strategist specializing in neurotechnology and BCI (brain-computer interface) careers. The user does NOT have a PhD - avoid suggesting roles that require one (Research Scientist at most companies requires a PhD). The user has: extensive PyTorch experience, 7+ years of EEG/ECoG/iEEG signal processing pipeline development, authored the sliceTCA tensor decomposition library for neural data, contributed to MNE-Python, BME background with medical device hardware experience, and a BCI paper in preparation.

Given a job title, skill, or BCI concept, return ONLY a JSON object with exactly three keys:
- "titles": array of up to 12 alternative job title strings to search for. Prioritize non-PhD tracks: Research Engineer, ML Engineer, Software Engineer (Neuro/BCI), Applied Scientist, Signal Processing Engineer, Neurotech Engineer, Systems Engineer, Data Scientist. Avoid "Research Scientist" unless it is documented that the role does not require a PhD.
- "keywords": array of up to 12 technical keywords, skills, or domain terms to include in job searches that will surface more relevant listings.
- "sectors": array of up to 12 specific company types, industry verticals, or named employers/labs where these roles exist without PhD requirements.
Return ONLY valid JSON. No markdown, no explanation, no preamble."""


_TECH_BAR_SCORE_SYSTEM = """You are a technical-hiring screener for a senior engineer/analyst targeting clinical and health roles. The candidate wants ANY role with a genuine TECHNICAL or QUANTITATIVE component — NOT only machine learning. Given a job posting (title + description), rate the role's TECHNICAL BAR on a 0.0-to-1.0 scale.

Scoring rubric:
- HIGH (0.75-1.0): the core work is hands-on technical — writing software; building or maintaining data pipelines, databases, ETL, or infrastructure; quantitative/statistical analysis; modeling, algorithms, or research; quality, test, validation, or systems engineering; data management or data engineering; bioinformatics/computational work. The person builds, engineers, analyzes, or rigorously tests.
- MEDIUM (0.4-0.7): partially technical — an analyst/specialist who runs existing tools or queries rather than building them, or a role mixing technical tasks with coordination/admin.
- LOW (0.0-0.35): little or no technical component — executing SOPs, coordinating or monitoring clinical studies, site monitoring, regulatory paperwork, manual data ENTRY, medical scribing, scheduling, patient care/nursing, recruiting subjects, sales, marketing, or general people/project management without technical depth.

Key distinctions: "data management" / "data engineering" / "quality engineering" / "test engineering" / "validation" / "analysis" are TECHNICAL (high-ish). "data ENTRY" / "study coordination" / "site monitoring" / "scribing" / "nursing" are NOT (low). Judge by the ACTUAL responsibilities, not the title or seniority. A "Scientist" who only coordinates studies is LOW; a "Quality Engineer" who builds validation tooling is HIGH.

Also classify the employer's MISSION into exactly one tier:
- "healthcare-tech": the company's core product/mission is healthcare, medical, clinical, digital health, medical devices, diagnostics, or health-data/health-AI. (The candidate's top preference.)
- "health-bio-science": a broader health-adjacent, biotech, life-sciences, genomics/omics, pharma, scientific-software/instruments, or research-science mission that isn't primarily a healthcare product.
- "community-driven-tech": an open-source, developer-tools/platform, or community-driven technology company (e.g. Red Hat, GitHub, GitLab, Mozilla, Hugging Face).
- "other": none of the above (generic SaaS, fintech, e-commerce, defense, etc. that isn't community-driven/open-source).

Return ONLY a JSON object with exactly these keys:
- "score": a number from 0.0 to 1.0 (two decimals) — the TECHNICAL BAR.
- "mission": one of "healthcare-tech", "health-bio-science", "community-driven-tech", or "other".
- "reason": one short phrase (<= 12 words) naming the deciding factor.
Return ONLY valid JSON. No markdown, no preamble."""


_LOCATION_EXPAND_SYSTEM = """You are a geographic search strategist. Given a location term (a city, region, country, or qualifier like "remote"), return ONLY a JSON object with exactly two keys:
- "include": array of up to 15 related location strings that should ALSO match when filtering jobs for this area. Examples: for "North Carolina", include "NC", "Durham", "Raleigh", "Chapel Hill", "Research Triangle", "RTP". For "remote", include "work from home", "wfh", "fully remote", "distributed", "anywhere".
- "exclude": array of up to 8 location strings that should be explicitly excluded when someone specifies this search. Examples: for "us only", include common offshore locations the user likely wants to filter out.
Use lowercase unless the token is normally capitalized (country codes etc). Return ONLY valid JSON, no markdown, no explanation."""


DISCOVER_SYSTEM = """You are a technical recruiter who maps employers to ATS platforms. Given a sector, industry, or job concept, list companies that (a) plausibly hire for roles in that space and (b) are likely to post jobs publicly. The user is targeting neurotechnology / BCI / ML / signal-processing roles and does NOT have a PhD.

Return ONLY a JSON object with this exact shape:
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
- Up to 15 companies. Prefer ones where a Research Engineer / ML Engineer / Software Engineer role is achievable without a PhD.
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
    return call_claude_json(_BCI_EXPAND_SYSTEM, term)


def expand_location(term):
    return call_claude_json(_LOCATION_EXPAND_SYSTEM, term)


# --------------------------------------------------------------------------- #
#  Technical-bar scorer (repurposes the --expand Claude call).                 #
#                                                                              #
#  Instead of expanding a term into more keywords, this asks Claude to score   #
#  a single posting 0.0-1.0 on how much real model/algorithm/research work it  #
#  involves (high) vs. SOP-execution / study-coordination / data-entry (low).  #
# --------------------------------------------------------------------------- #

# Mission tiers, highest alignment → lowest. "community-driven-tech" ranks
# just above the catch-all "other": open-source / developer-tooling employers
# (Red Hat, GitHub, …) aren't health/bio, but are a better cultural fit than
# generic SaaS/fintech, so they sit one notch up.
_MISSION_TIERS = ("healthcare-tech", "health-bio-science",
                  "community-driven-tech", "other")

# Tiers whose companies are crawled. Only "other" is dropped — everything
# above it (including community-driven-tech) is kept active. Single source of
# truth for the active/inactive decision across the sourcing + scoring passes.
ACTIVE_MISSION_TIERS = ("healthcare-tech", "health-bio-science",
                        "community-driven-tech")


_COMPANY_MISSION_SYSTEM = """You score how well an EMPLOYER matches a specific candidate's ideal target, from 0.0 to 1.0. Given a company name + sample postings, judge the COMPANY (not one role).

The candidate is a BME / neuroscience / ML data engineer. Their BULLSEYE is neurotechnology — brain-computer interfaces (BCI), neural implants/interfaces, electrophysiology, neural signal processing, and neuro-focused medical devices — broadening out to medical devices, clinical/health ML & AI, diagnostics, and digital health, then to biotech / pharma R&D / genomics / life-sciences.

Return ONLY a JSON object with exactly:
- "mission": one of
    "healthcare-tech"       (healthcare, medical, clinical, digital health, medical devices, diagnostics, neurotech/BCI, or health data/AI),
    "health-bio-science"    (biotech, pharma, life sciences, genomics/omics, scientific tools/instruments, research science),
    "community-driven-tech" (open-source, developer tools/platforms, or community-driven technology — e.g. Red Hat, GitHub, GitLab, Mozilla, Hugging Face, the Linux Foundation),
    "other"                 (generic SaaS, fintech, ecommerce, defense, staffing, etc. — not community-driven/open-source).
- "score": 0.0-1.0 alignment with the candidate's target:
    * 1.00 EXACTLY = a brain-computer-interface / neural-implant / neural-interface / neurotechnology company. This is the single highest score, reserved ONLY for this category — award a full 1.00, never 0.9x.
    * 0.85-0.98  = other neuro, medical-device, signal-processing, clinical or health ML/AI, diagnostics, or digital-health company.
    * 0.60-0.85  = biotech / pharma R&D / genomics / science-heavy life-sciences company.
    * 0.40-0.65  = pharma/biologics manufacturing, CRO clinical-operations, or health services with limited technical or neural overlap.
    * 0.22-0.38  = community-driven / open-source / developer-tooling company (Red Hat, GitHub, …) — not health/bio, but a better fit than generic tech.
    * 0.00-0.20  = anything else (generic SaaS, fintech, ecommerce, defense, staffing).
- "reason": one short phrase (<= 12 words).
Return ONLY valid JSON. No markdown, no preamble."""


_RESUME_FIT_SYSTEM = """You score how well a specific JOB fits a specific CANDIDATE, from 0.0 to 1.0, for a targeted job search.

The candidate's strengths, in priority order:
1. ML / neural-data / signal-processing engineering (PyTorch, CUDA, tensor methods, GPU pipelines).
2. Scientific data & pipeline engineering (ETL, data standards, SQLite, de-identification, reproducibility, CI/CD, HPC/cloud).
3. Medical-device design-controls exposure (academic DHF, ISO 14971, 510(k) strategy) and the data-integrity layer of an FDA IDE study.

Treat (3) as SUPPORTING context, not a primary qualification: it is academic design-controls and standards-mapping exposure plus regulated-research data work, NOT production quality operations. Do not let regulatory keyword overlap outrank a genuine (1) or (2) match.

Weigh: overlap of the job's requirements with the strengths above (in that priority order); seniority match; whether the candidate clears the bar without being wildly overqualified. Penalize hard mismatches (requires a PhD; a stack/domain with no overlap; wrong seniority).

Hard caps:
- If the role's CORE is production quality operations (IQ/OQ/PQ, CAPA, GMP manufacturing QA), clinical study operations, or regulatory affairs without engineering content: cap fit at 0.45 regardless of keyword overlap.
- If the job description is missing or trivially short: judge from the title alone, cap fit at 0.45, and include "no description" in the reason.

Return ONLY a JSON object with exactly:
- "fit": 0.0-1.0 (two decimals).
- "reason": one short phrase (<= 14 words) naming the deciding factor.
Return ONLY valid JSON. No markdown, no preamble."""


def score_company_mission(name, context=""):
    """Return (mission_tier|None, score|None, reason) for an employer."""
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
    # Deterministic bullseye anchor: a genuine BCI / neural-interface company
    # is the candidate's exact target and must score 1.0 (the defined top of
    # the scale) — not a hedged 0.9x. Match the NAME only: the reason text is
    # where the model's *negations* live ("no neurotech focus", "not a BCI
    # company"), and a substring match there would flip a rejection into a
    # perfect score.
    if re.search(r"brain[-\s]?computer|brain[-\s]?machine|\bbci\b|neural (?:implant|"
                 r"interface|prosthe)|neurotech|neural[-\s]?signal",
                 name.lower()):
        score, tier = 1.0, "healthcare-tech"
    return tier, score, reason


def score_resume_fit(resume, title, description=""):
    """Return (fit: float in [0,1]|None, reason: str) for one job vs a résumé."""
    if not resume:
        return None, ""
    desc = (description or "")[:2200]
    user = (f"CANDIDATE RÉSUMÉ:\n{resume[:6000]}\n\n"
            f"JOB TITLE: {title}\nJOB DESCRIPTION:\n{desc or '(no description)'}")
    result = call_claude_json(_RESUME_FIT_SYSTEM, user, max_tokens=120)
    if not result or "fit" not in result:
        return None, ""
    try:
        fit = max(0.0, min(1.0, float(result["fit"])))
    except (TypeError, ValueError):
        return None, ""
    return fit, str(result.get("reason", "")).strip()


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
