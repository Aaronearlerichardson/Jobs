"""The `comeet` board spec."""

from __future__ import annotations

from src.rows import JSON

# Comeet careers pages (www.comeet.com/jobs/<slug>/<company uid>): the page
# itself carries every open position as plain JSON in its `COMPANY_POSITIONS_DATA`
# script variable (one page, no paging), so no API token is needed. The listing
# names a position's office, department, workplace type and body, with only an
# update time (no posting date). www.comeet.com/robots.txt allows /jobs.
SPEC: dict[str, JSON] = {
    "detect": [{"host": "comeet.com",
                "re": [r"(?i)//(?:www\.)?comeet\.(?:com|co)/jobs/([a-z0-9][a-z0-9_-]*)/"
                       r"([0-9a-f]{2}\.[0-9a-f]{3})(?![0-9a-z])"],
                "blocklist": ["sitemap"],
                "careers_url": "https://www.comeet.com/jobs/{slug}/{cid}"}],
    "canary": {"name": "CHEQ", "handle": "cheq|65.005", "min_jobs": 2},
    "handle": {"parts": ["slug", "cid"]},
    "job_ref": {"re": r"(?i)//(?:www\.)?comeet\.(?:com|co)/jobs/([a-z0-9][a-z0-9_-]*)/"
                      r"([0-9a-f]{2}\.[0-9a-f]{3})/[^/?#]+/([0-9a-f]{2}\.[0-9a-f]{3})",
                "parts": ["slug", "cid", "jid"]},
    "listing": {
        "url": "https://www.comeet.com/jobs/{slug}/{cid}",
        "decoder": {"kind": "json_in_html", "regex": r"COMPANY_POSITIONS_DATA\s*=\s*"},
        "fields": {
            "id": {"format": "comeet_{slug}_{uid}"},
            "title": "name",
            "url": "url_comeet_hosted_page",
            "location": {"join": ["location.name", "location.country"], "sep": ", ",
                         "default": "Unknown"},
            "description": {"of": "custom_fields.details[0].value", "transform": "html_text"},
            "remote_hint": {"const": "comeet:remote", "when": {"eq": ["workplace_type", "Remote"]}},
            "department": "department",
        },
    },
    # No detail endpoint and the list is one page: a position is open while
    # the listing names it.
    "closure": {"via": "listing"},
}
