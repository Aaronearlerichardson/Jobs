"""Every per-platform fact about an ATS board, as data.

`BOARDS[ats]` says how a store row names a board, how its listing is read
and mapped to rows, how one posting is read back, and how a posting's
closure is judged. The engine that reads it is `src.ats.fetchers.board`
(whose `validate_spec` is the schema); nothing else in `src/` may name a
platform.

The literal is JSON-compatible on purpose (str, int, float, bool, None,
list, dict; regexes as strings): tests/test_invariants.py pins
`json.loads(json.dumps(BOARDS)) == BOARDS`, so moving it to a JSON file
later is mechanical.
"""

BOARDS = {
    "greenhouse": {
        "sweep": True,
        "prunable": True,
        "guess": True,
        "job_ref": {"re": r"greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_.-]+)/jobs/(\d+)",
                    "parts": ["slug", "jid"]},
        "listing": {
            "url": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
            "probe_url": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
            "decoder": {"kind": "json", "entries": "jobs"},
            "fields": {
                "id": {"format": "gh_{slug}_{id}"},
                "title": "title",
                "url": "absolute_url",
                "location": {"merge": {"primary": "location.name", "extras": "offices[].name"},
                             "default": "Unknown"},
                "description": {"of": "content", "transform": "unescape_html_text"},
                "posted_at": {"first": ["first_published", "updated_at"]},
                "remote_hint": {"const": "greenhouse:office",
                                "when": {"contains": ["offices[].name", "remote"]}},
                "department": {"join": ["departments[].name"]},
            },
        },
        "detail": {
            "url": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}?content=true",
            "fields": {"description": {"of": "content", "transform": "unescape_html_text"}},
            "location": "never",
        },
        "employer": "company_name",
    },
    "lever": {
        "sweep": True,
        "prunable": True,
        "guess": True,
        "job_ref": {"re": r"lever\.co/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{20,})",
                    "parts": ["slug", "jid"]},
        "listing": {
            "url": "https://api.lever.co/v0/postings/{slug}?mode=json",
            "decoder": {"kind": "json", "entries": ""},
            "fields": {
                "id": {"format": "lv_{slug}_{id}"},
                "title": "text",
                "url": "hostedUrl",
                "location": {"merge": {"primary": "categories.location",
                                       "extras": {"first": ["categories.allLocations",
                                                            "allLocations"]}},
                             "default": "Unknown"},
                "description": "descriptionPlain",
                "posted_at": "createdAt",
                "remote_hint": {"const": "lever:workplaceType",
                                "when": {"eq": ["workplaceType", "remote"]}},
                "department": "categories.team",
            },
        },
        "detail": {
            "url": "https://api.lever.co/v0/postings/{slug}/{jid}",
            "fields": {"description": "descriptionPlain"},
            "location": "never",
        },
    },
    "ashby": {
        "sweep": True,
        "prunable": True,
        "guess": True,
        "job_ref": {"re": r"ashbyhq\.com/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{20,})",
                    "parts": ["slug", "jid"]},
        "listing": {
            "url": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
            # The posting API says "jobs"; only the embed payload says "jobPostings".
            "decoder": {"kind": "json", "entries": ["jobs", "jobPostings"]},
            "fields": {
                "id": {"format": "ashby_{slug}_{id}"},
                "title": "title",
                "url": {"first": ["jobUrl", {"format": "https://jobs.ashbyhq.com/{slug}/{id}"}]},
                "location": {"merge": {"primary": "location",
                                       "extras": "secondaryLocations[].location"},
                             "default": "Unknown"},
                "description": "descriptionPlain",
                "posted_at": {"first": ["publishedDate", "publishedAt"]},
                "remote_hint": {"const": "ashby:isRemote",
                                "when": {"any": [{"eq": ["isRemote", True]},
                                                 {"eq": ["workplaceType", "remote"]}]}},
                "department": {"join": ["department", "team"]},
            },
        },
        # No per-posting endpoint: closure is board membership.
        "closure": {"via": "listing"},
    },
}
