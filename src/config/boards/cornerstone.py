"""The `cornerstone` board spec.

Notes:
    Endpoint shapes credited to kalil0321/ats-scrapers (MIT). The board is
    the tenant's host and career-site number. The search API lives on a
    regional cloud host and wants the bearer token the site's home page
    embeds (good about six hours).
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "csod.com",
                "re": [r"(?i)([a-z0-9-]+\.csod\.com)/ux/ats/careersite/(\d+)"],
                "transform": ["lower", "keep"],
                "blocklist": ["www.csod.com", "help.csod.com", "community.csod.com"]}],
    "canary": {"name": "MACOM", "handle": "macomtech.csod.com|4", "min_jobs": 20},
    "handle": {"parts": ["host", "site"],
               "prelude": [{"url": "https://{host}/ux/ats/careersite/{site}/home?c={host|host_label}",
                            "decoder": {"kind": "json_in_html", "regex": r"csod\.context\s*=\s*"},
                            "set": {"token": "token", "cloud": "endpoints.cloud"}}],
               "why": "the search API is on a regional host and wants the page's "
                      "own bearer token, 2026-10"},
    "job_ref": {"re": r"(?i)^https?://([a-z0-9-]+\.csod\.com)/ux/ats/careersite/(\d+)"
                      r"/(?:home/requisition|job)/(\d+)",
                "parts": ["host", "site", "jid"]},
    "listing": {
        "method": "POST",
        "url": "{cloud}rec-job-search/external/jobs",
        "headers": {"Authorization": "Bearer {token}"},
        "json": {"careerSiteId": "{site}", "careerSitePageId": "{site}",
                 "pageNumber": "$page", "pageSize": "$size", "cultureId": 1,
                 "cultureName": "en-US"},
        "decoder": {"entries": "data.requisitions"},
        "pager": {"kind": "page", "size": 100, "start": 1, "pages": 40,
                  "total": "data.totalCount"},
        "fields": {
            "id": {"format": "cornerstone_{host|host_label}_{site}_{requisitionId}"},
            "title": "displayJobTitle",
            "url": {"format": "https://{host}/ux/ats/careersite/{site}/home/requisition/"
                              "{requisitionId}?c={host|host_label}"},
            "location": {"merge": {"primary": {"join": ["locations[0].city",
                                                        "locations[0].state",
                                                        "locations[0].country"],
                                               "sep": ", "},
                                   "extras": {"each": "locations",
                                              "do": {"join": ["city", "state", "country"],
                                                     "sep": ", "}}}},
            "description": {"of": "externalDescription", "transform": "unescape_html_text"},
        },
    },
    # The posting's page carries its JSON-LD while it is live; a closed
    # requisition's serves the bare shell with a 200.
    "detail": {
        "url": "https://{host}/ux/ats/careersite/{site}/home/requisition/{jid}"
               "?c={host|host_label}",
        "decoder": {"kind": "jsonld"},
        "fields": {"description": "description"},
    },
    "closure": {"open": {"truthy": "title"},
                "unmatched": "requisition page no longer carries the posting",
                "why": "a closed requisition's page answers 200 without its posting, 2026-10"},
}
