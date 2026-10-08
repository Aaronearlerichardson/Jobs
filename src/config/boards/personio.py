"""The `personio` board spec.

Personio career pages, read from the tenant's public XML feed.

Notes:
    Added 2026-10-07; moved to the native xml decoder the same day
    (e6d31c1). A description is every `jobDescription` section; a
    location is the main office then `additionalOffices`. A pulled
    posting answers 404.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "jobs.personio.",
                "re": [r"(?i)([a-z0-9][a-z0-9-]*)\.jobs\.personio\.(?:de|com)"],
                "blocklist": ["www", "app", "api", "support", "developer", "help"],
                "careers_url": "https://{slug}.jobs.personio.de"}],
    "canary": {"name": "CLARK", "handle": "clark", "min_jobs": 3},
    "job_ref": {"re": r"(?i)//([a-z0-9][a-z0-9-]*)\.jobs\.personio\.(?:de|com)/job/(\d+)"},
    "listing": {
        "url": "https://{slug}.jobs.personio.de/xml",
        "decoder": {"kind": "xml", "select": "position",
                    "lists": ["office", "jobDescription"]},
        "fields": {
            "id": {"format": "personio_{slug}_{id}"},
            "title": "name",
            "url": {"format": "https://{slug}.jobs.personio.de/job/{id}"},
            "location": {"merge": {"primary": "office[0]", "extras": "additionalOffices.office"}},
            "description": {"join": [{"each": "jobDescriptions.jobDescription",
                                      "do": {"format": "{name}\n{value}"}}],
                            "sep": "\n\n", "transform": "html_text"},
            "posted_at": "createdAt",
            "department": "department",
        },
    },
    # Closure only: the posting's page, a pulled posting answers 404.
    "detail": {
        "url": "https://{slug}.jobs.personio.de/job/{jid}",
        "decoder": {"kind": "html", "select": "body"},
    },
}
