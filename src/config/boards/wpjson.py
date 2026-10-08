"""The `wpjson` board spec.

Notes:
    A WordPress theme's careers route, keyed on any page of the site. A
    posting's URL is its outbound apply page on the applicant portal's
    host, so there is no job_ref.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "canary": {"name": "restor3d", "handle": "https://www.restor3d.com/company/careers/"},
    "sweep": True,
    # A WordPress theme's careers route, keyed on any page of the site.
    "handle": {"columns": ["careers_url"], "parts": ["site"]},
    "listing": {
        "url": "{site|origin}/wp-json/post-filters-archive/get-posts",
        "params": {"post_type": "career", "posts_per_page": "$size", "paged": "$page"},
        "decoder": {"entries": "posts"},
        # Every page declares the last.
        "pager": {"kind": "page", "size": 100, "pages": 50, "start": 1,
                  "declared": {"of": "max_num_pages", "transform": "int", "default": 1}},
        "fields": {
            "_site": {"format": "{site|host_nowww}"},
            "id": {"format": "wpjson_{_site}_{ID}"},
            "title": {"of": "post_title", "transform": "one_line", "default": "Unknown"},
            "url": {"first": ["link.url", "permalink"]},
            "location": {"join": [{"of": "location.city", "transform": "one_line"},
                                  {"of": "location.state", "transform": "one_line"}],
                         "sep": ", ", "default": "See posting"},
            "posted_at": "post_date",
        },
    },
    # A posting's URL is its outbound apply page on the applicant
    # portal's own host, so no job_ref: the detail reads the row's URL.
    "detail": {
        "url": "{url}",
        "decoder": {"kind": "html",
                    "select": ["#portalViewRequirement", "[class*='bmportalrequirementdetails']"]},
        "fields": {"description": "text"},
    },
    "closure": {"via": "page"},
}
