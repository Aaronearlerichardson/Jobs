"""The `pinpoint` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "pinpointhq.com", "re": [r"(?i)([a-z0-9][a-z0-9-]*)\.pinpointhq\.com"],
                "blocklist": ["www", "app", "api", "help", "support", "blog", "developers"],
                "careers_url": "https://{slug}.pinpointhq.com"}],
    "canary": {"name": "ISG", "handle": "isginc", "min_jobs": 10},
    # A posting's URL names its uuid, not the id its row carries: closure
    # by board membership, on the row id.
    "job_ref": {"re": r"(?i)//([a-z0-9][a-z0-9-]*)\.pinpointhq\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?postings/",
                "parts": ["slug"]},
    "listing": {
        "url": "https://{slug}.pinpointhq.com/postings.json",
        "decoder": {"entries": "data"},
        "fields": {
            "id": {"format": "pinpoint_{slug}_{id}"},
            "title": "title",
            "url": "url",
            # A name is "City, ST" or a bare "City": the province completes the latter.
            "location": {"first": [{"of": "location.name",
                                    "when": {"contains": ["location.name", ","]}},
                                   {"join": ["location.name", "location.province"], "sep": ", "},
                                   {"const": "Remote", "when": {"eq": ["workplace_type", "remote"]}}],
                         "default": "Unknown"},
            "description": {"join": ["description", "key_responsibilities",
                                     "skills_knowledge_expertise"], "sep": "\n",
                            "transform": "html_text"},
            "remote_hint": {"const": "pinpoint:workplace_type",
                            "when": {"eq": ["workplace_type", "remote"]}},
            "department": "job.department.name",
        },
    },
    "closure": {"via": "listing"},
}
