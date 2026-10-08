"""The `smartrecruiters` board spec.

Notes:
    The API answers a slug in any case alike, so the handle folds
    (2026-10-08). Boards run to thousands of rows, so it stays out of the
    lightweight sweep. A pulled posting answers 200 with active=false; a
    repost answers under its successor's id, so neither open nor closed.
    The vendor's `oneclick-ui` apply widget embedded on customer pages is
    blocklisted from detection (2026-10-08).
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "smartrecruiters.com",
                "re": [r"(?i)(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"],
                # The vendor's apply widget, embedded on customer pages.
                "blocklist": ["oneclick-ui"]},
               {"host": "smartrecruiters.com",
                "re": [r"(?i)api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9]+)/"]}],
    "canary": {"name": "Eurofins", "handle": "Eurofins"},
    # The API answers a slug in any case alike (2026-10-08).
    "handle": {"fold": True},
    "discovery": {"search": [[5, "jobs.smartrecruiters.com"]], "hint": [[6, "smartrecruiters"]]},
    # Not in the lightweight sweep: boards run to thousands of rows.
    "job_ref": {"re": r"smartrecruiters\.com/([A-Za-z0-9_.-]+)/(\d+)", "parts": ["slug", "id"]},
    "listing": {
        "url": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        "params": {"limit": "$size", "offset": "$offset"},
        "decoder": {"entries": "content"},
        "pager": {"kind": "offset", "size": 100, "total": "totalFound"},
        "fields": {
            "id": {"format": "sr_{slug}_{id}"},
            "title": "name",
            "url": {"format": "https://jobs.smartrecruiters.com/{slug}/{id}"},
            "location": {"join": ["location.city", "location.region", "location.country"],
                         "sep": ", "},
            "posted_at": "releasedDate",
            "department": {"join": ["department.label", "function.label"]},
        },
    },
    "detail": {
        "url": "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}",
        "fields": {"description": {"join": ["jobAd.sections.jobDescription.text",
                                            "jobAd.sections.qualifications.text",
                                            "jobAd.sections.additionalInformation.text"],
                                   "transform": "html_text"}},
    },
    # A pulled posting answers 200 with active=false. A repost answers
    # under its successor's id, which only postingUrl carries: a verdict
    # on the successor, so neither open nor closed for this row.
    "closure": {"closed": [{"when": {"eq": ["active", False]}, "why": {"const": "active=false"}}],
                "open": [{"when": {"all": [{"eq": ["active", True]},
                                           {"any": [{"falsy": "id"}, {"falsy": "postingUrl"},
                                                    {"contains": ["postingUrl", "$id"]}]}]},
                          "why": {"const": "active"}}]},
    "employer": "company.name",
}
