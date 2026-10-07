"""The `teamtailor` board spec."""

from __future__ import annotations

from src.rows import JSON

# Teamtailor: the tenant's JSON Feed (`/jobs.json`, JSON Feed 1.1 with a
# schema.org posting beside each item), endpoint shape credited to
# kalil0321/ats-scrapers (MIT). The handle is the tenant's host, so a
# tenant on its own domain is a board too once something names it. The
# RSS twin (`/jobs.rss`) also names the state ("Raleigh, North Carolina,
# United States"), a remote status and a department, but is RSS `item`s
# no decoder reads; the feed's places are city and country code.
SPEC: dict[str, JSON] = {
    "detect": [{"host": "teamtailor.com", "re": [r"(?i)([a-z0-9-]+\.teamtailor\.com)"],
                "blocklist": ["www.teamtailor.com", "app.teamtailor.com", "api.teamtailor.com",
                              "support.teamtailor.com", "help.teamtailor.com",
                              "docs.teamtailor.com", "blog.teamtailor.com"],
                "careers_url": "https://{slug}/jobs"}],
    "canary": {"name": "Slater Consult", "handle": "slaterconsult.teamtailor.com", "min_jobs": 3},
    "job_ref": {"re": r"(?i)^https?://([a-z0-9-]+\.teamtailor\.com)/jobs/(\d+)"},
    "listing": {
        "url": "https://{slug}/jobs.json",
        # A board over 100 postings names the next page's URL, as JSON Feed does.
        "pager": {"kind": "cursor", "size": 100, "pages": 40,
                  "next": "next_url", "has_next": "next_url"},
        "decoder": {"entries": "items"},
        "fields": {
            "_jid": {"of": "url", "transform": r"group:/jobs/(\d+)"},
            "id": {"format": "teamtailor_{slug|host_key}_{_jid}", "when": {"truthy": "_jid"}},
            "title": "title",
            "url": "url",
            "location": {"merge": {"primary": {"join": ["_jobposting.jobLocation[0].address.addressLocality",
                                                        "_jobposting.jobLocation[0].address.addressRegion",
                                                        "_jobposting.jobLocation[0].address.addressCountry"],
                                               "sep": ", "},
                                   "extras": {"each": "_jobposting.jobLocation",
                                              "do": {"join": ["address.addressLocality",
                                                              "address.addressRegion",
                                                              "address.addressCountry"],
                                                     "sep": ", "}}},
                         "default": "Unknown"},
            "description": {"of": "content_html", "transform": "html_text"},
            "posted_at": "date_published",
        },
    },
    # Closure only (the feed carries the body): the posting's own page,
    # where the bare id redirects to the slugged one and a pulled posting
    # answers 404. Its JSON-LD holds raw line breaks in a string, which
    # no JSON parser takes.
    "detail": {
        "url": "https://{slug}/jobs/{jid}",
        "decoder": {"kind": "html", "select": "body"},
    },
}
