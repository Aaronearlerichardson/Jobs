"""The `jazzhr` board spec.

Notes:
    Postings carry JSON-LD where present, 60 pages a pull. A redirect to
    the vendor's job-seekers page reads as 404, not an empty board (20
    phantom boards a pass, 2026-10-08).
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "applytojob.com", "re": [r"(?i)([a-z0-9-]+)\.applytojob\.com"]}],
    "canary": {"name": "Cyclotron Research Centre", "handle": "cyclotroninc"},
    "sweep": True,
    "prunable": True,
    "job_ref": {"re": r"(?i)^(https?://([a-z0-9-]+)\.applytojob\.com/apply/([A-Za-z0-9]+)[^?#]*)",
                "parts": ["link", "slug", "jid"]},
    "listing": {
        "url": "https://{slug}.applytojob.com/",
        "missing_at": r"(?i)^https?://(?:www\.)?jazzhr\.com/",
        "decoder": {"kind": "html", "select": "a[href*='/apply/']", "context": ["li"],
                    "cells": {"location": "li:has(.fa-map-marker)"}},
        "fields": {
            "_path": {"of": "href", "transform": "group:(/apply/[A-Za-z0-9]+/[A-Za-z0-9_-]+)"},
            "_url": {"format": "https://{slug}.applytojob.com{_path}"},
            # The key a posting's JSON-LD gives it: none names an
            # identifier, so its URL's.
            "_key": {"of": "_url", "transform": "stable_id"},
            "id": {"format": "jsonld_{slug}_{_key}", "when": {"truthy": "_path"}},
            "title": {"of": "text", "when": {"truthy": "_path"}},
            "url": "_url",
            "location": "location",
        },
    },
    # Each posting page's JSON-LD, where it carries one, 60 pages a pull.
    "rescue": {"when": "always", "unknown": "", "cap": 60,
               "fields": ["location", "description", "posted_at", "remote_hint"],
               "why": "the index names no body or date; a posting's JSON-LD does, 2026-09"},
    "detail": {
        "url": "{link}",
        # A page with no JSON-LD posting: its body container.
        "decoder": {"kind": "jsonld", "cells": {"description": "#job-description"}},
        "record": ["postings[0]", "page"],
        "fields": {
            # "Unknown" where a posting names no place; a bare page names none.
            "location": {"first": ["location",
                                   {"const": "Unknown", "when": {"truthy": "title"}}]},
            "description": "description",
            "posted_at": "posted_at",
            "remote_hint": {"const": "jsonld:telecommute", "when": {"truthy": "telecommute"}},
        },
        "location": "if_unknown",
    },
    "closure": {"url": "https://{slug}.applytojob.com/apply/{jid}",
                "why": "a pulled posting's page still answers 200, its slug-free apply "
                       "URL 410, 2026-09"},
}
