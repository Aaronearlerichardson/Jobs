"""The `dayforce` board spec.

Notes:
    Endpoint shapes credited to kalil0321/ats-scrapers (MIT). The board is
    the client and its board code (a client can run several); the search
    API wants the CSRF token and cookie from the site's auth route. No
    detail endpoint: a pulled posting's page answers 404.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "dayforcehcm.com",
                # A locale leads the path; a client is never one.
                "re": [r"(?i)jobs\.dayforcehcm\.com/(?:[a-z]{2,3}-[a-z0-9]{2,4}/)?"
                       r"(?![a-z]{2,3}-[a-z0-9]{2,4}/)([a-z0-9_-]+)/([a-z0-9_-]+)"],
                "transform": ["lower", None],
                "blocklist": ["api", "_next", "static"]}],
    "canary": {"name": "Impact Fire Services", "handle": "aifire|IMPACT", "min_jobs": 10},
    "handle": {"parts": ["client", "board"],
               "prelude": [{"url": "https://jobs.dayforcehcm.com/api/auth/csrf",
                            "set": {"csrf": "csrfToken"}}],
               "why": "the search API refuses a POST without the CSRF token and its "
                      "cookie, 2026-10"},
    "job_ref": {"re": r"(?i)^https?://jobs\.dayforcehcm\.com/(?:[a-z]{2,3}-[a-z0-9]{2,4}/)?"
                      r"([a-z0-9_-]+)/([a-z0-9_-]+)/jobs/(\d+)",
                "parts": ["client", "board", "jid"]},
    "listing": {
        "method": "POST",
        "url": "https://jobs.dayforcehcm.com/api/geo/{client}/jobposting/search",
        "headers": {"X-CSRF-TOKEN": "{csrf}"},
        "json": {"clientNamespace": "{client}", "jobBoardCode": "{board}",
                 "cultureCode": "en-US", "distanceUnit": 0, "paginationStart": "$offset"},
        "decoder": {"entries": "jobPostings"},
        # The server serves 25 a page and names none of it in the request.
        "pager": {"kind": "offset", "size": 25, "pages": 60, "total": "maxCount"},
        # No detail endpoint: a pulled posting's page answers 404.
        "fields": {
            "id": {"format": "dayforce_{client}_{jobPostingId}"},
            "title": "jobTitle",
            "url": {"format": "https://jobs.dayforcehcm.com/en-US/{client}/{board}"
                              "/jobs/{jobPostingId}"},
            "location": {"merge": {"primary": {"join": ["postingLocations[0].cityName",
                                                        "postingLocations[0].stateCode"],
                                               "sep": ", "},
                                   "extras": {"each": "postingLocations",
                                              "do": {"join": ["cityName", "stateCode"],
                                                     "sep": ", "}}}},
            "description": {"of": "jobDescription", "transform": "unescape_html_text"},
            "posted_at": "postingStartTimestampUTC",
            "remote_hint": {"const": "dayforce:virtual",
                            "when": {"eq": ["hasVirtualLocation", True]}},
        },
    },
}
