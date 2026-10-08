"""The `recruitee` board spec.

Notes:
    Endpoint shape credited to kalil0321/ats-scrapers (MIT). An offer's URL
    names its slug, not the row id, so closure is board membership.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "recruitee.com", "re": [r"(?i)([a-z0-9][a-z0-9-]*)\.recruitee\.com"],
                "blocklist": ["www", "app", "api", "help", "support", "blog", "status"],
                "careers_url": "https://{slug}.recruitee.com"}],
    "canary": {"name": "Hudson Manpower", "handle": "hudsonmanpower", "min_jobs": 10},
    # An offer's URL names the offer's slug, not the id its row carries:
    # closure by board membership, on the row id.
    "job_ref": {"re": r"(?i)//([a-z0-9][a-z0-9-]*)\.recruitee\.com/o/", "parts": ["slug"]},
    "listing": {
        "url": "https://{slug}.recruitee.com/api/offers/",
        "decoder": {"entries": "offers"},
        "fields": {
            "id": {"format": "recruitee_{slug}_{id}"},
            "title": "title",
            "url": "careers_url",
            "location": {"first": [{"merge": {"primary": "location",
                                              "extras": {"each": "locations",
                                                         "do": {"join": ["city", "state", "country"],
                                                                "sep": ", "}}}},
                                   {"const": "Remote", "when": {"truthy": "remote"}}]},
            "description": {"join": ["description", "requirements"], "sep": "\n",
                            "transform": "html_text"},
            "posted_at": {"first": ["published_at", "created_at"]},
            "remote_hint": {"const": "recruitee:remote", "when": {"truthy": "remote"}},
            "department": "department",
        },
    },
    "closure": {"via": "listing"},
}
