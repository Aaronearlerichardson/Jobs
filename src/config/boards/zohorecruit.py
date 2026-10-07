"""The `zohorecruit` board spec."""

from __future__ import annotations

from src.rows import JSON

# Zoho Recruit career sites (<slug>.zohorecruit.com/jobs/Careers): the page
# carries its postings as entity-escaped JSON in the `value` of a hidden
# `<input id="jobs">` (read by the json_in_html decoder's `element`). The page
# serves at most 50 postings and offers no way to ask for more (`?page=2`
# answers the same 50, the site's script has no listing request, and its RSS
# feed is removed), so the pager reads a 50-row snapshot as capped: a board
# of fewer rows is complete. Only the .zohorecruit.com host is detected; the
# regional hosts (.eu, .in, .com.au) are not robots-checked.
# <slug>.zohorecruit.com/robots.txt allows /jobs. A listing row names a
# posting's city, state and country (absent on remote ones), industry, open
# date and plain-text body.
SPEC: dict[str, JSON] = {
    "detect": [{"host": "zohorecruit.com",
                "re": [r"(?i)//([a-z0-9][a-z0-9-]*)\.zohorecruit\.com/(?:jobs|careers)\b"],
                "blocklist": ["www", "accounts", "recruit", "static", "help", "css", "creator"],
                "careers_url": "https://{slug}.zohorecruit.com/jobs/Careers"}],
    "canary": {"name": "PSC Biotech", "handle": "biotech", "min_jobs": 10},
    "job_ref": {"re": r"(?i)//([a-z0-9][a-z0-9-]*)\.zohorecruit\.com/jobs/[a-z0-9_-]+/(\d+)"},
    "listing": {
        "url": "https://{slug}.zohorecruit.com/jobs/Careers",
        "params": {"page": "$page"},
        "decoder": {"kind": "json_in_html", "element": "input#jobs"},
        "pager": {"kind": "page", "size": 50, "pages": 2, "ceiling": 50,
                  "bare_first": True,
                  "why": "the page serves 50 postings and ignores ?page=, so a full "
                         "page is a capped snapshot, 2026-10"},
        "fields": {
            "id": {"format": "zohorecruit_{slug}_{id}"},
            "title": "Posting_Title",
            "url": {"format": "https://{slug}.zohorecruit.com/jobs/Careers/{id}"},
            "location": {"first": [{"join": ["City", "State", "Country"], "sep": ", "},
                                   {"const": "Remote", "when": {"truthy": "Remote_Job"}}],
                         "default": "Unknown"},
            "description": {"of": "Job_Description", "transform": "html_text"},
            "posted_at": "Date_Opened",
            "remote_hint": {"const": "zohorecruit:remote", "when": {"truthy": "Remote_Job"}},
            "department": "Industry",
        },
    },
    # Closure only: the posting's page by its id alone. A pulled posting
    # answers 200 with a bare page that carries no `Posting_Title`.
    "detail": {
        "url": "https://{slug}.zohorecruit.com/jobs/Careers/{jid}",
        "decoder": {"kind": "html", "select": "body"},
        "record": [""],
    },
    "closure": {"open": {"contains": ["page", "Posting_Title"]},
                "unmatched": "posting page no longer carries the posting",
                "why": "a pulled posting's page answers 200 without its posting, 2026-10"},
}
