"""The `eightfold` board spec.

Notes:
    Endpoint shapes credited to kalil0321/ats-scrapers (MIT). A tenant's
    API wants its company domain, which the host does not name: the first
    TLD that answers is the domain. The table's detection-only entries
    follow it; Eightfold, Taleo and Avature became fetchable in 2026-10.
"""

from __future__ import annotations

from src.rows import JSON

# Detection-only platforms: real ATSes discovery recognises but cannot
# fetch (bot-protected APIs or JS-only boards). A lead's detection
# names a host or path for the note; an entry with no `re` claims the
# vendor's host and detects nothing. Eightfold, Taleo and Avature among
# them are fetchable since 2026-10, left in place: this order is
# detection order (signatures.detect).
SPEC: dict[str, JSON] = {
    "detect": [{"host": "eightfold.ai", "re": [r"(?i)([a-z0-9-]+\.eightfold\.ai)"],
                "blocklist": ["www.eightfold.ai", "app.eightfold.ai", "apply.eightfold.ai",
                              "docs.eightfold.ai", "support.eightfold.ai"]}],
    "canary": {"name": "Arcadis", "handle": "arcadis.eightfold.ai"},
    "eager": True,
    "handle": {"try": {"domain": ["{slug|host_label}.com", "{slug|host_label}.org",
                                  "{slug|host_label}.net"]},
               "accept": {"status": [200]},
               "why": "the API's domain is the employer's own, 404 on any other, 2026-10"},
    "job_ref": {"re": r"(?i)^https?://([a-z0-9-]+\.eightfold\.ai)/careers/job/(\d+)"},
    "listing": [
        {
            "url": "https://{slug}/api/pcsx/search",
            "params": {"domain": "{domain}", "start": "$offset"},
            "decoder": {"entries": "data.positions"},
            # The server sizes its pages (10); the walk learns it.
            "pager": {"kind": "offset", "pages": 60, "total": "data.count"},
            "fields": {
                "id": {"format": "eightfold_{slug|host_label}_{id}"},
                "title": "name",
                "url": {"format": "https://{slug}/careers/job/{id}"},
                "location": {"join": ["locations[]"], "sep": "; "},
                "posted_at": "postedTs",
                "remote_hint": {"const": "eightfold:workLocationOption",
                                "when": {"eq": ["workLocationOption", "remote"]}},
                "department": "department",
            },
        },
        {
            "url": "https://{slug}/api/apply/v2/jobs",
            "decoder": {"entries": "positions"},
            "pager": {"kind": "offset", "pages": 60, "total": "count"},
            "fields": {
                "id": {"format": "eightfold_{slug|host_label}_{id}"},
                "title": "name",
                "url": {"format": "https://{slug}/careers/job/{id}"},
                "location": {"join": ["locations[]"], "sep": "; "},
                "posted_at": "t_create",
                "remote_hint": {"const": "eightfold:workLocationOption",
                                "when": {"eq": ["work_location_option", "remote"]}},
                "department": "department",
            },
            "why": "a tenant without PCSX answers the first 403 and serves this API, 2026-10",
        },
    ],
    # Answers on both APIs; a posting that is gone is a 404.
    "detail": {
        "url": "https://{slug}/api/apply/v2/jobs/{jid}",
        "params": {"domain": "{domain}"},
        "fields": {"description": {"of": "job_description", "transform": "html_text"}},
    },
}
