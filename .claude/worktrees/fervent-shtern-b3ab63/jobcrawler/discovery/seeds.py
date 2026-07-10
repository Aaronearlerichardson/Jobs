"""
Curated seed companies to merge into discovery results.

These are employers Claude's training data systematically misses for a
given region — anchors in RTP's biotech / med-device / NC-tech corridor
that rarely make it into Claude's top suggestions. Seeds are probed
exactly like Claude candidates (validate_candidate sweeps all four
ATSes when ats='unknown'), so you don't need to know the ATS upfront.

Activation: seeds merge in only when the discovery term matches one of
RTP_TRIGGERS below. Untriggered runs ('AI startups' with no geo term)
skip seeds entirely so non-RTP discovery stays clean.

Adding entries: append to RTP_SEED_COMPANIES with just a name and an
optional short note. Don't bother guessing the ATS or slug — the
universal probe will find it if it's on greenhouse/lever/ashby/kula.
Employers on workday (Red Hat, BD, LabCorp, United Therapeutics, etc.)
will land in the 'unconfirmed' bucket with their careers URL intact,
which is still useful as a manual-investigate list.
"""

import re

# Case-insensitive triggers. Short tokens (<= 3 chars) use word-boundary
# matching so "nc" in RTP_TRIGGERS won't false-match "neuroscience".
RTP_TRIGGERS: tuple[str, ...] = (
    "rtp", "raleigh", "durham", "chapel hill", "cary", "morrisville",
    "apex", "triangle", "research triangle", "north carolina", "nc",
)

_SHORT_TOKEN_LEN = 3


def _matches_term(term: str) -> bool:
    if not term:
        return False
    t = term.lower()
    for trig in RTP_TRIGGERS:
        if len(trig) <= _SHORT_TOKEN_LEN:
            if re.search(rf"\b{re.escape(trig)}\b", t):
                return True
        elif trig in t:
            return True
    return False


# Edit this list to curate seeds. Keep notes short — they render in the
# discovery report's 'unconfirmed' table and help future-you remember
# why a company is on the list.
RTP_SEED_COMPANIES: list[dict] = [
    # --- Biotech / pharma ---
    {"name": "United Therapeutics",               "notes": "RTP biotech HQ"},
    {"name": "Precision BioSciences",             "notes": "Durham gene editing"},
    {"name": "Humacyte",                          "notes": "Durham regenerative med"},
    {"name": "AskBio",                            "notes": "RTP gene therapy (Bayer)"},
    {"name": "Locus Biosciences",                 "notes": "RTP phage therapeutics"},
    {"name": "Seqirus",                           "notes": "Holly Springs vaccines"},
    {"name": "bioMerieux",                        "notes": "Durham diagnostics"},

    # --- Medical devices / health systems ---
    {"name": "BD",                                "notes": "RTP medical devices"},
    {"name": "LabCorp",                           "notes": "Burlington NC diagnostics"},
    {"name": "Atrium Health",                     "notes": "Charlotte NC health"},

    # --- CDMOs / bio manufacturing ---
    {"name": "FUJIFILM Diosynth Biotechnologies", "notes": "Morrisville biopharma CDMO"},

    # --- Tech — RTP-anchored ---
    {"name": "Red Hat",                           "notes": "Raleigh open-source"},
    {"name": "Epic Games",                        "notes": "Cary games/Unreal"},
    {"name": "NetApp",                            "notes": "RTP storage"},
    {"name": "Pendo",                             "notes": "Raleigh product analytics"},
    {"name": "Bandwidth",                         "notes": "Raleigh CPaaS"},
    {"name": "Pryon",                             "notes": "Raleigh enterprise AI"},
    {"name": "Relias",                            "notes": "Cary learning/compliance"},
    {"name": "Spreedly",                          "notes": "Durham payments"},
    {"name": "WillowTree",                        "notes": "Durham digital products"},

    # --- add more RTP seeds here ---
]


def seed_candidates_for(term: str) -> list[dict]:
    """
    Return raw candidate dicts to merge with Claude's discovery output,
    or [] if `term` doesn't mention anything in RTP_TRIGGERS.

    Dicts have the same shape as Claude's payload entries, so they flow
    through candidate_from_dict / validate_candidate unchanged.
    """
    if not _matches_term(term):
        return []
    return [
        {
            "name":        s["name"],
            "ats":         "unknown",     # Fix B sweeps all four probes
            "slug_guess":  None,
            "careers_url": "",
            "notes":       f"[seed] {s.get('notes', '')}".strip(),
        }
        for s in RTP_SEED_COMPANIES
    ]
