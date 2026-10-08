"""The `phenom` board spec.

Notes:
    The tenant's own site is the board, so no vendor host names it; every
    page embeds its widget API origin, the handle. The listing lives under
    a locale prefix only the root redirect names; a tenant whose bare root
    answers 403 (2026-10) serves its locale paths, tried next. Last in
    the table: its URLs are on the tenant's own host.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    # The tenant's own site is the board, so no vendor host names it:
    # every page embeds its widget API origin, the handle.
    "detect": [{"re": [r'(?i)"widgetApiEndpoint"\s*:\s*"https?://([a-z0-9.-]+)/widgets"']}],
    "canary": {"name": "PPD", "handle": "jobs.thermofisher.com"},
    # The listing lives under a locale prefix only the board's root
    # redirect names (/us/en, /global/en, ...). A tenant whose bare root
    # answers 403 (Lilly, 2026-10) serves its locale paths: those are tried
    # next, the page URL they land on being the base.
    "handle": {"follow": {"base": ["{slug}", "{slug}/us/en", "{slug}/global/en"]}},
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
