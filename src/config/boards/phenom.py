"""The `phenom` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    # The tenant's own site is the board, so no vendor host names it:
    # every page embeds its widget API origin, the handle.
    "detect": [{"re": [r'(?i)"widgetApiEndpoint"\s*:\s*"https?://([a-z0-9.-]+)/widgets"']}],
    "canary": {"name": "PPD", "handle": "jobs.thermofisher.com"},
    # The listing lives under a locale prefix only the board's root
    # redirect names (/us/en, /global/en, ...).
    "handle": {"follow": {"base": "{slug}"}},
    # Last in this table: its URLs are the ones on the tenant's own host.
    "job_ref": {"re": r"^(https?://([^/?#]+)/[a-z]{2,8}/[a-z]{2}(?:[-_][A-Za-z]{2})?)"
                      r"/job/([^/?#]+)/?$",
                "parts": ["base", "slug", "reqId"]},
    "listing": {
        "url": "{base}/search-results",
        "params": {"from": "$offset", "size": "$size"},
        "decoder": {"kind": "json_in_html", "regex": r"phApp\.ddo\s*=\s*",
                    "entries": "eagerLoadRefineSearch.data.jobs"},
        # The server caps size at 500.
        "pager": {"kind": "overlap", "size": 500, "step": 250, "pages": 40,
                  "total": "eagerLoadRefineSearch.totalHits",
                  "why": "row order shifts between requests, carrying rows across page "
                         "boundaries, 2026-09"},
        "fields": {
            "_req": {"first": ["reqId", "jobId"]},
            "_key": {"format": "{slug}", "transform": "host_key"},
            "id": {"format": "phenom_{_key}_{_req}"},
            "title": "title",
            "url": {"format": "{base}/job/{_req}"},
            "location": {"first": ["location", "cityStateCountry", "cityState",
                                   {"join": ["city", "state", "country"], "sep": ", "}]},
            "posted_at": "postedDate",
            "department": "category",
        },
    },
    "detail": {
        "url": "{base}/job/{reqId}",
        "decoder": {"kind": "json_in_html", "regex": r"phApp\.ddo\s*=\s*"},
        "record": "jobDetail.data.job",
        "fields": {
            "description": {"of": "description", "transform": "html_text"},
            "location": {"first": ["location", "cityStateCountry", "cityState",
                                   {"join": ["city", "state", "country"], "sep": ", "},
                                   {"join": ["standardised_multi_location[]"
                                             ".standardisedMapQueryLocation"], "sep": "; "}]},
        },
        "location": "always",
    },
    "closure": {"via": "page"},
}
