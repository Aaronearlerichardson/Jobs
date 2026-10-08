"""The `rippling` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "rippling.com", "re": [r"(?i)ats\.rippling\.com/([a-z0-9][a-z0-9-]+)/jobs"]}],
    "canary": {"name": "Blackrock Neurotech", "handle": "blackrockneurotech"},
    "sweep": True,
    "eager": True,
    "job_ref": {"re": r"rippling\.com/([^/]+)/jobs/([0-9a-f-]{36})"},
    "listing": {
        "url": "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs",
        "decoder": {"entries": ["", "jobs"]},
        "fields": {
            "id": {"format": "rippling_{slug}_{uuid:12}"},
            "title": "name",
            "url": {"first": ["url", {"format": "https://ats.rippling.com/{slug}/jobs/{uuid}"}]},
            "location": {"first": ["workLocation.label",
                                   {"join": ["workLocations[]"], "sep": ", ", "max": 3}]},
            "department": {"first": ["department.label", "department"]},
        },
    },
    "detail": {
        "url": "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{jid}",
        # `description` is a {role, company} pair of HTML: the role first.
        "fields": {"description": {"first": [{"join": ["description.role", "description.company"]},
                                             "description"],
                                   "transform": "html_text"}},
    },
}
