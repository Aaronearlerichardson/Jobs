"""The `manatal` board spec.

Manatal career pages, read through the page's own JSON API.

Notes:
    Added 2026-10-07. 20 a page whatever `page_size` asks; carries
    location, body and organization (department), no date. The host
    serves no robots.txt (404). A pulled posting's page answers 404.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "careers-page.com",
                "re": [r"(?i)careers-page\.com/(?:api/v1\.0/c/)?([a-z0-9][a-z0-9_-]*)"],
                "blocklist": ["api", "static", "login", "signup", "pricing"],
                "careers_url": "https://www.careers-page.com/{slug}"}],
    "canary": {"name": "Manatal", "handle": "manatal", "min_jobs": 10},
    "job_ref": {"re": r"(?i)careers-page\.com/([a-z0-9][a-z0-9_-]*)/job/([A-Za-z0-9]+)"},
    "listing": {
        "url": "https://www.careers-page.com/api/v1.0/c/{slug}/jobs/",
        "params": {"page": "$page", "page_size": "$size"},
        "decoder": {"entries": "results"},
        "pager": {"kind": "page", "size": 20, "start": 1, "pages": 40, "total": "count"},
        "fields": {
            "id": {"format": "manatal_{slug}_{hash}"},
            "title": "position_name",
            "url": {"format": "https://www.careers-page.com/{slug}/job/{hash}"},
            "location": {"of": "location_display"},
            "description": {"of": "description", "transform": "html_text"},
            "department": "organization_name",
        },
    },
    # Closure only (the listing carries the body): the posting's page, which
    # answers 404 once pulled.
    "detail": {
        "url": "https://www.careers-page.com/{slug}/job/{jid}",
        "decoder": {"kind": "html", "select": "body"},
    },
}
