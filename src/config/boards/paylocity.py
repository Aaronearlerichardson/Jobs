"""The `paylocity` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    # The board URL's name segment after the company GUID is cosmetic.
    "detect": [{"host": "paylocity.com",
                "re": [r"(?i)recruiting\.paylocity\.com/[Rr]ecruiting/[Jj]obs/All/([0-9a-fA-F-]{36})"]}],
    "canary": {"name": "United Imaging - North America",
               "handle": "d527ad39-680d-45fa-9178-38a81898aec2"},
    "sweep": True,
    "eager": True,
    "job_ref": {"re": r"(?i)recruiting\.paylocity\.com/Recruiting/Jobs/Details/(\d+)",
                "parts": ["jid"]},
    "listing": {
        # The slug is the company GUID; the trailing name segment is cosmetic.
        "url": "https://recruiting.paylocity.com/recruiting/jobs/All/{slug}/x",
        "decoder": {"kind": "json_in_html", "regex": r"pageData\s*=\s*", "entries": "Jobs"},
        "fields": {
            "id": {"format": "paylocity_{slug:8}_{JobId}"},
            "title": "JobTitle",
            "url": {"format": "https://recruiting.paylocity.com/Recruiting/Jobs/Details/{JobId}"},
            "location": {"first": ["LocationName",
                                   {"join": ["JobLocation.City", "JobLocation.State"], "sep": ", "},
                                   {"const": "Remote", "when": {"truthy": "IsRemote"}},
                                   "JobLocation.Country"],
                         "default": "Unknown"},
            # No "description": the listing's Description is a teaser cut at
            # ~110 characters (2026-09-23); the body is the detail page.
            "remote_hint": {"const": "paylocity:isRemote", "when": {"truthy": "IsRemote"}},
            "department": "HiringDepartment",
        },
    },
    "detail": {
        "url": "https://recruiting.paylocity.com/Recruiting/Jobs/Details/{jid}",
        "decoder": {"kind": "html", "select": ".job-preview-details, [class*=job-preview]"},
        # The page's "Apply <title> <location> Apply" chrome leads the body.
        "fields": {"description": {"of": "text", "transform": "after_marker:Description"}},
    },
    # A pulled posting's detail page still answers 200 (2026-09-23).
    "closure": {"via": "page"},
}
