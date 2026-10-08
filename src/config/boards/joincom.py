"""The `joincom` board spec.

JOIN company pages: a Next.js page whose `__NEXT_DATA__` holds the postings.

Notes:
    Added 2026-10-07. Five postings to a `?page=N` page, each page naming
    the last. The listing names city, country, workplace type and
    category, no body. A pulled posting answers 404.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "join.com", "re": [r"(?i)//(?:www\.)?join\.com/companies/([a-z0-9][a-z0-9-]*)"],
                "blocklist": ["sitemap", "sitemap-jobs-index"],
                "careers_url": "https://join.com/companies/{slug}"}],
    "canary": {"name": "Mazars", "handle": "mazars", "min_jobs": 5},
    "job_ref": {"re": r"(?i)//(?:www\.)?join\.com/companies/([a-z0-9][a-z0-9-]*)/(\d+)"},
    "listing": {
        "url": "https://join.com/companies/{slug}",
        "params": {"page": "$page"},
        "decoder": {"kind": "json_in_html", "regex": r'<script id="__NEXT_DATA__"[^>]*>\s*',
                    "entries": "props.pageProps.initialState.jobs.items"},
        "pager": {"kind": "page", "size": 5, "start": 1, "pages": 40,
                  "declared": "props.pageProps.initialState.jobs.pagination.pageCount"},
        "fields": {
            # The number a posting's URL leads with, which is not its `id`.
            "_jid": {"of": "idParam", "transform": r"group:^(\d+)"},
            "id": {"format": "joincom_{slug}_{_jid}", "when": {"truthy": "_jid"}},
            "title": "title",
            "url": {"format": "https://join.com/companies/{slug}/{idParam}"},
            "location": {"first": [{"join": ["city.cityName", "city.countryName"], "sep": ", "},
                                   {"const": "Remote", "when": {"eq": ["workplaceType", "REMOTE"]}}]},
            "posted_at": "createdAt",
            "remote_hint": {"const": "joincom:remote", "when": {"eq": ["workplaceType", "REMOTE"]}},
            "department": "category.name",
        },
    },
    # Closure only: the posting's page by its id alone, which redirects to
    # the slugged one; a pulled posting answers 404.
    "detail": {
        "url": "https://join.com/companies/{slug}/{jid}",
        "decoder": {"kind": "html", "select": "body"},
    },
}
