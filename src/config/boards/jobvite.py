"""The `jobvite` board spec.

Notes:
    Some tenants replace the all-rows page with a landing page listing
    nothing, which is why the search goes first.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "jobvite.com", "re": [r"(?i)jobs\.jobvite\.com/([a-z0-9][a-z0-9_-]*)"]}],
    "canary": {"name": "Neogenomics", "handle": "neogenomics"},
    "discovery": {"search": [[6, "jobs.jobvite.com"]]},
    "sweep": True,
    "eager": True,
    "handle": {"parts": ["tenant"]},
    "job_ref": {"re": r"(?i)//jobs\.jobvite\.com/([a-z0-9][a-z0-9_-]*)/job/([A-Za-z0-9]+)",
                "parts": ["tenant", "jid"]},
    "listing": [
        {
            "url": "https://jobs.jobvite.com/{tenant|lower}/search",
            "params": {"p": "$page"},
            "decoder": {"kind": "html",
                        "select": "a.jv-job-list-name[href*='/{tenant}/job/']",
                        "context": ["li"], "cells": {"location": ".jv-job-list-location"}},
            "pager": {"kind": "page", "size": 50, "pages": 20},
            "fields": {
                "_tenant": {"format": "{tenant}", "transform": "lower"},
                "_jid": {"of": "href", "transform": "group:(?i)/job/([A-Za-z0-9]+)"},
                "id": {"format": "jv_{_tenant}_{_jid}"},
                "title": "text",
                "url": {"format": "https://jobs.jobvite.com/{_tenant}/job/{_jid}"},
                "location": "location",
            },
        },
        # Every row on one page; some tenants replace it with a landing
        # page listing nothing, which is why the search goes first.
        {"url": "https://jobs.jobvite.com/{tenant|lower}/jobs", "reset": ["params", "pager"],
         "why": "a tenant whose search lists nothing may list every row here, "
                "2026-09 (inferred)"},
    ],
    "detail": {
        "url": "https://jobs.jobvite.com/{tenant}/job/{jid}",
        "decoder": {"kind": "jsonld"},
        "fields": {
            "description": {"of": "description", "transform": "one_line"},
            "location": "location",
            "posted_at": "posted_at",
            "remote_hint": {"const": "jsonld:telecommute", "when": {"truthy": "telecommute"}},
        },
        "location": "if_unknown",
    },
    "closure": {"via": "page"},
}
