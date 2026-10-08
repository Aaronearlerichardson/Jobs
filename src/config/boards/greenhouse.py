"""The `greenhouse` board spec.

Notes:
    The API answers a slug in any case alike, so the handle folds
    (2026-10-08).
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "greenhouse.io",
                "re": [r"(?i)(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"]}],
    "canary": {"name": "Databricks", "handle": "databricks"},
    # The API answers a slug in any case alike (2026-10-08).
    "handle": {"fold": True},
    "discovery": {"search": [[1, "boards.greenhouse.io"], [2, "job-boards.greenhouse.io"]],
                  "hint": [[2, "greenhouse"]]},
    "sweep": True,
    "prunable": True,
    "guess": True,
    "job_ref": {"re": r"greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_.-]+)/jobs/(\d+)"},
    "listing": {
        "url": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        "probe_url": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
        "decoder": {"entries": "jobs"},
        "fields": {
            "id": {"format": "gh_{slug}_{id}"},
            "title": "title",
            "url": "absolute_url",
            "location": {"merge": {"primary": "location.name", "extras": "offices[].name"}},
            "description": {"of": "content", "transform": "unescape_html_text"},
            "posted_at": {"first": ["first_published", "updated_at"]},
            "remote_hint": {"const": "greenhouse:office",
                            "when": {"contains": ["offices[].name", "remote"]}},
            "department": {"join": ["departments[].name"]},
        },
    },
    "detail": {
        "url": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}?content=true",
        "fields": {"description": {"of": "content", "transform": "unescape_html_text"}},
    },
    "employer": "company_name",
}
