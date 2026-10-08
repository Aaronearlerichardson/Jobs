"""The `ashby` board spec.

Notes:
    The API answers a slug in any case alike, so the handle folds
    (2026-10-08). No per-posting endpoint: closure is board membership.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "ashbyhq.com", "re": [r"(?i)jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)"]}],
    "canary": {"name": "Vanta", "handle": "vanta"},
    # The API answers a slug in any case alike (2026-10-08).
    "handle": {"fold": True},
    "discovery": {"search": [[4, "jobs.ashbyhq.com"]], "hint": [[4, "ashbyhq"]]},
    "sweep": True,
    "prunable": True,
    "guess": True,
    "job_ref": {"re": r"ashbyhq\.com/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{20,})"},
    "listing": {
        "url": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        # The posting API says "jobs"; only the embed payload says "jobPostings".
        "decoder": {"entries": ["jobs", "jobPostings"]},
        "fields": {
            "id": {"format": "ashby_{slug}_{id}"},
            "title": "title",
            "url": {"first": ["jobUrl", {"format": "https://jobs.ashbyhq.com/{slug}/{id}"}]},
            "location": {"merge": {"primary": "location",
                                   "extras": "secondaryLocations[].location"}},
            "description": "descriptionPlain",
            "posted_at": {"first": ["publishedDate", "publishedAt"]},
            "remote_hint": {"const": "ashby:isRemote",
                            "when": {"any": [{"eq": ["isRemote", True]},
                                             {"eq": ["workplaceType", "remote"]}]}},
            "department": {"join": ["department", "team"]},
        },
    },
    # No per-posting endpoint: closure is board membership.
    "closure": {"via": "listing"},
}
