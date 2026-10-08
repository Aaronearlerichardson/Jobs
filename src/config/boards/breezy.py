"""The `breezy` board spec.

Notes:
    Breezy, Recruitee and Pinpoint: public JSON, endpoint shapes credited
    to kalil0321/ats-scrapers (MIT).
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "breezy.hr", "re": [r"(?i)([a-z0-9][a-z0-9-]*)\.breezy\.hr"],
                "blocklist": ["www", "app", "api", "help", "support", "blog"],
                "careers_url": "https://{slug}.breezy.hr"}],
    "canary": {"name": "Highlights Healthcare", "handle": "highlights-healthcare",
               "min_jobs": 10},
    "eager": True,
    "job_ref": {"re": r"(?i)//([a-z0-9][a-z0-9-]*)\.breezy\.hr/p/([0-9a-f]+)"},
    "listing": {
        "url": "https://{slug}.breezy.hr/json",
        "fields": {
            "id": {"format": "breezy_{slug}_{id}"},
            "title": "name",
            "url": "url",
            "location": {"first": [{"merge": {"primary": "location.name",
                                              "extras": "locations[].name"}},
                                   {"const": "Remote", "when": {"truthy": "location.is_remote"}}]},
            "posted_at": "published_date",
            "remote_hint": {"const": "breezy:is_remote",
                            "when": {"truthy": "location.is_remote"}},
            "department": "department",
        },
    },
    # The listing names no body; the posting page's JSON-LD does. A pulled
    # posting's page still answers 200 (the board's own), so closure is
    # board membership.
    "detail": {
        "url": "https://{slug}.breezy.hr/p/{jid}",
        "decoder": {"kind": "jsonld"},
        "fields": {"description": "description"},
    },
    "closure": {"via": "listing"},
}
