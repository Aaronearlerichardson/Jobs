"""The `workday` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    # The CXS API URL first (the tenant appears twice), then any board
    # URL; the site slot after an optional locale, never an API or
    # asset segment.
    "detect": [{"host": "myworkdayjobs.com",
                "re": [r"(?i)https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com"
                       r"/wday/cxs/[a-z0-9-]+/([A-Za-z0-9_-]+)/"],
                "transform": ["lower", "int", None]},
               {"host": "myworkdayjobs.com",
                "re": [r"(?i)https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com"
                       r"(?:/[a-z]{2}-[A-Z]{2})?/([A-Za-z0-9_-]+)"],
                "transform": ["lower", "int", None],
                "blocklist": ["wday", "cxs", "api", "static", "assets", "login"]}],
    "canary": {"name": "ThermoFisher Scientific IT",
               "handle": "thermofisher|5|ThermoFisherCareers"},
    # A (tenant, pod, site) triple no name guess reaches; a parent's
    # tenant can list its subsidiaries' postings (Danaher's, Genedata's);
    # the host serves most large employers.
    "discovery": {"scan": True, "shared": True, "narrow": True,
                  "search": [[9, "myworkdayjobs.com"]],
                  "hint": [[1, "myworkdayjobs"], [8, "workday"]]},
    # Not in the lightweight sweep: boards run to thousands of rows and
    # are pulled scoped to the locality.
    "handle": {"columns": ["handle"],
               "parts": ["tenant", "pod", "site"],
               "fold": True,
               "try": {"cxs_tenant": ["{tenant}", "{tenant|underscore}"]},
               "accept": {"status": [200], "total": True},
               "why": "a hyphenated tenant's CXS path takes the underscore form; "
                      "the hyphen form 422s, 2026-08"},
    # The URL's site slot can hold a locale; the company row's wins.
    "job_ref": {"re": r"(?i)^https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com"
                      r"(?:/[a-z]{2}(?:-[A-Za-z]{2})?)?/([^/?#]+)(/job/[^?#]*)",
                "parts": ["tenant", "pod", "site", "path"]},
    "listing": {
        "method": "POST",
        "url": "https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{cxs_tenant}/{site}/jobs",
        "json": {"appliedFacets": "$facets", "searchText": "$search_text",
                 "limit": "$size", "offset": "$offset"},
        "headers": {"Content-Type": "application/json"},
        "decoder": {"entries": "jobPostings"},
        # Only page 0 reports the total.
        "pager": {"kind": "offset", "size": 20, "pages": 60, "total": "total",
                  "ceiling": 2000,
                  "why": "the API serves 2000 rows at most and reports a bigger "
                         "board as 2000, 2026-09"},
        "scope": {"kind": "facets", "facets": "facets", "param": "facetParameter",
                  "param_re": "(?i)location|country|region|city|state",
                  "values": "values", "id": "id", "label": "descriptor"},
        "fields": {
            "_pid": {"first": [{"of": "externalPath", "transform": "group:([^/]*)$"},
                               {"of": "title", "transform": "stable_id"}]},
            "id": {"format": "wd_{tenant}_{_pid}"},
            "title": "title",
            "url": {"format": "https://{tenant}.wd{pod}.myworkdayjobs.com/en-US/{site}"
                              "{externalPath}",
                    "when": {"truthy": "externalPath"},
                    "else": {"format": "https://{tenant}.wd{pod}.myworkdayjobs.com"}},
            "location": "locationsText",
            "posted_at": {"first": ["postedOnDate", "postedOn"]},
            # A listing entry carries six keys, none a department (2026-09-23).
            "department": None,
        },
    },
    # A posting's path names one of its sites (`free`).
    "rescue": {"unknown": r"(?i)^\s*\d+\s+locations?\s*$", "cap": 150, "cache_days": 3,
               "free": {"of": {"of": "externalPath", "transform": "group:^/job/([^/]+)/"},
                        "transform": "dash_space"},
               "why": 'a multi-site posting lists as "N Locations", naming no place, 2026-08'},
    "detail": {
        "url": "https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{cxs_tenant}/{site}{path}",
        "record": "jobPostingInfo",
        "fields": {
            "description": {"of": "jobDescription", "transform": "html_text"},
            "location": {"join": ["location", "additionalLocations[]"], "sep": "; "},
            "remote_hint": {"const": "workday:remoteType",
                            "when": {"any": [{"eq": ["remoteType", "Remote"]},
                                             {"eq": ["remoteType", "Fully Remote"]}]}},
        },
        "location": "if_unknown",
    },
    # A pulled posting can also answer 403 "S22"; its page then renders
    # no posting (60 listed live, 63 pulled, 2026-09-28).
    "closure": {"open": {"any": [{"truthy": "jobDescription"}, {"truthy": "title"}]},
                "page_closed": r"postingAvailable:\s*false",
                "unmatched": "no posting record",
                "why": "a pulled posting's record answers 200 without a title or a body, "
                       "2026-08"},
}
