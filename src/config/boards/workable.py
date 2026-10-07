"""The `workable` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "workable.com",
                "re": [r"(?i)apply\.workable\.com/(?:api/v\d+/widget/accounts/)?([a-z0-9][a-z0-9_-]*)"]}],
    "canary": {"name": "It Practice", "handle": "practicetek"},
    # Not in the lightweight sweep (it seeds LOCAL); set "sweep" to add it.
    "eager": True,
    # The tenant-path posting URL names both coordinates; the listing's
    # own short link (/j/<shortcode>) names no account.
    "job_ref": {"re": r"(?i)^https?://apply\.workable\.com/([A-Za-z0-9][A-Za-z0-9_-]*)/j/([A-Za-z0-9]+)"},
    "listing": {
        "url": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
        "decoder": {"entries": "jobs"},
        "fields": {
            "id": {"format": "workable_{slug}_{shortcode}"},
            "title": "title",
            "url": {"format": "https://apply.workable.com/{slug}/j/{shortcode}/"},
            "location": {"first": [
                {"merge": {"primary": {"join": ["city", "state", "country"], "sep": ", "},
                           "extras": {"each": "locations",
                                      "do": {"join": ["city", "region", "country"], "sep": ", "},
                                      "skip": {"truthy": "hidden"}}}},
                {"const": "Remote", "when": {"truthy": "telecommuting"}}],
                "default": "Unknown"},
            "posted_at": {"first": ["published_on", "created_at"]},
            "remote_hint": {"const": "workable:telecommuting",
                            "when": {"eq": ["telecommuting", True]}},
            "department": "department",
        },
    },
    "detail": {
        "url": "https://apply.workable.com/api/v1/accounts/{slug}/jobs/{jid}",
        # `requirements` is the part the fit model reads; `benefits` is
        # per-board boilerplate and stays out.
        "fields": {"description": {"join": ["description", "requirements"], "sep": "\n",
                                   "transform": "html_text"}},
    },
}
