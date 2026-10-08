"""The `lever` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "lever.co", "re": [r"(?i)jobs\.lever\.co/([a-z0-9_-]+)"]}],
    "canary": {"name": "Veeva", "handle": "veeva"},
    "discovery": {"search": [[3, "jobs.lever.co"]], "hint": [[3, "lever"]]},
    "sweep": True,
    "prunable": True,
    "guess": True,
    "job_ref": {"re": r"lever\.co/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{20,})"},
    "listing": {
        "url": "https://api.lever.co/v0/postings/{slug}?mode=json",
        "fields": {
            "id": {"format": "lv_{slug}_{id}"},
            "title": "text",
            "url": "hostedUrl",
            "location": {"merge": {"primary": "categories.location",
                                   "extras": {"first": ["categories.allLocations",
                                                        "allLocations"]}}},
            "description": "descriptionPlain",
            "posted_at": "createdAt",
            "remote_hint": {"const": "lever:workplaceType",
                            "when": {"eq": ["workplaceType", "remote"]}},
            "department": "categories.team",
        },
    },
    "detail": {
        "url": "https://api.lever.co/v0/postings/{slug}/{jid}",
        "fields": {"description": "descriptionPlain"},
    },
}
