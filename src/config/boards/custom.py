"""The `custom` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "canary": {"name": "Microsoft", "handle": "https://microsoft.ai/careers/"},
    "sweep": True,
    # A self-hosted careers page, read by the careers-page reader
    # (src.ats.board.custom).
    "handle": {"columns": ["careers_url"], "parts": ["page"]},
    "listing": {
        "url": "{page}",
        "decoder": {"kind": "html", "select": "$job_links"},
        "fields": {
            "_key": {"of": "url", "transform": "url_key:48"},
            "id": {"format": "custom_{_key}"},
            "title": "title",
            "url": "url",
            "location": "location",
            "department": None,
        },
    },
}
