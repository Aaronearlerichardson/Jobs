"""The `personio` board spec."""

from __future__ import annotations

from src.rows import JSON

# Personio career pages (<slug>.jobs.personio.de): the tenant's public XML feed
# (`/xml`, a `workzag-jobs` of `position`s). No decoder reads that XML as
# records, so the html decoder reads it as markup: each position's `id`
# element is the match and its parent the context the cells read, with tag
# names lowercased by the HTML parser. The feed's bodies sit in CDATA, which
# markup drops, so a posting carries no description here; the office is the
# position's first (`additionalOffices` are not read).
SPEC: dict[str, JSON] = {
    "detect": [{"host": "jobs.personio.",
                "re": [r"(?i)([a-z0-9][a-z0-9-]*)\.jobs\.personio\.(?:de|com)"],
                "blocklist": ["www", "app", "api", "support", "developer", "help"],
                "careers_url": "https://{slug}.jobs.personio.de"}],
    "canary": {"name": "CLARK", "handle": "clark", "min_jobs": 3},
    "job_ref": {"re": r"(?i)//([a-z0-9][a-z0-9-]*)\.jobs\.personio\.(?:de|com)/job/(\d+)"},
    "listing": {
        "url": "https://{slug}.jobs.personio.de/xml",
        "decoder": {"kind": "html", "select": "position > id", "context": "parent",
                    "cells": {"name": "name", "office": "office", "department": "department",
                              "created": "createdat"}},
        "fields": {
            "id": {"format": "personio_{slug}_{text}"},
            "title": "name",
            "url": {"format": "https://{slug}.jobs.personio.de/job/{text}"},
            "location": {"of": "office", "default": "Unknown"},
            "posted_at": "created",
            "department": "department",
        },
    },
    # Closure only: the posting's page, a pulled posting answers 404.
    "detail": {
        "url": "https://{slug}.jobs.personio.de/job/{jid}",
        "decoder": {"kind": "html", "select": "body"},
    },
}
