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
