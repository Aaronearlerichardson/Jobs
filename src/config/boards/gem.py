"""The `gem` board spec.

Gem's public job-board API, the whole board in one list.

Notes:
    Endpoint shape credited to kalil0321/ats-scrapers (MIT).
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "jobs.gem.com", "re": [r"(?i)jobs\.gem\.com/([a-z0-9_-]+)"],
                "blocklist": ["api", "embed", "static", "_next"],
                "careers_url": "https://jobs.gem.com/{slug}"}],
    "canary": {"name": "ResProp Management", "handle": "resprop", "min_jobs": 10},
    "job_ref": {"re": r"(?i)jobs\.gem\.com/([a-z0-9_-]+)/([A-Za-z0-9_-]{6,})"},
    "listing": {
        "url": "https://api.gem.com/job_board/v0/{slug}/job_posts/",
        "fields": {
            "id": {"format": "gem_{slug}_{id}"},
            "title": "title",
            "url": "absolute_url",
            "location": {"first": [{"merge": {"primary": "location.name",
                                              "extras": "offices[].location.name"}},
                                   {"const": "Remote", "when": {"eq": ["location_type", "remote"]}}]},
            "description": "content_plain",
            "posted_at": {"first": ["first_published_at", "created_at"]},
            "remote_hint": {"const": "gem:location_type",
                            "when": {"eq": ["location_type", "remote"]}},
            "department": {"join": ["departments[].name"]},
        },
    },
    "detail": {
        "url": "https://api.gem.com/job_board/v0/{slug}/job_posts/{jid}/",
        "fields": {"description": "content_plain"},
    },
}
