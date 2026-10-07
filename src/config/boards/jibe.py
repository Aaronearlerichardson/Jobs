"""The `jibe` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    # An iCIMS tenant's Jibe front, on the employer's own host. Ahead of
    # icims: its pages name the iCIMS tenant too, whose search the front
    # replaces with a script redirect.
    "detect": [{"re": [r'(?i)\b_jibe\s*=\s*\{\s*"cid"\s*:\s*"([a-z0-9_-]+)"'],
                "careers_url": "{page|origin}"}],
    "canary": {"name": "WakeMed", "handle": "https://jobs.wakemed.org"},
    # The board is the site's origin, keyed on any URL on it.
    "handle": {"columns": ["careers_url"], "parts": ["site"]},
    "listing": {
        "url": "{site|origin}/api/jobs",
        "params": {"page": "$page", "limit": "$size"},
        "decoder": {"entries": "jobs[].data"},
        "pager": {"kind": "page", "size": 100, "start": 1, "total": "totalCount"},
        "fields": {
            "_key": {"format": "{site}", "transform": "host_key"},
            "id": {"format": "jibe_{_key}_{req_id}"},
            "title": "title",
            "url": {"first": ["meta_data.canonical_url",
                              {"format": "{site|origin}/jobs/{slug}"}]},
            # Every place a posting names, "; "-joined.
            "location": {"first": ["full_location",
                                   {"join": ["city", "state", "country"], "sep": ", "}]},
            "description": {"join": ["description", "responsibilities", "qualifications"],
                            "sep": "\n", "transform": "html_text"},
            "posted_at": "posted_date",
            "department": {"join": ["categories[].name"], "sep": ", "},
        },
    },
    "employer": "hiring_organization",
}
