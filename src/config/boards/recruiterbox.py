"""The `recruiterbox` board spec.

Recruiterbox (Trakstar Hire): the public API wants a key, so the
server-rendered list is read.

Notes:
    25 cards a page with `?p=`. The old `<co>.recruiterbox.com` host
    redirects to `<co>.hire.trakstar.com`. The posting page's JSON-LD holds
    raw line breaks in a string (not valid JSON); a pulled posting's page
    answers 404.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "hire.trakstar.com",
                "re": [r"(?i)([a-z0-9][a-z0-9-]*)\.hire\.trakstar\.com"],
                "blocklist": ["www", "app", "api", "help", "support", "blog", "status"],
                "careers_url": "https://{slug}.hire.trakstar.com"},
               {"host": "recruiterbox.com",
                "re": [r"(?i)([a-z0-9][a-z0-9-]*)\.recruiterbox\.com"],
                "blocklist": ["www", "app", "api", "jobs", "help", "support", "blog", "status"],
                "careers_url": "https://{slug}.hire.trakstar.com"}],
    "canary": {"name": "Planate Management Group", "handle": "planate", "min_jobs": 10},
    "eager": True,
    "job_ref": {"re": r"(?i)//([a-z0-9][a-z0-9-]*)\.(?:hire\.trakstar|recruiterbox)\.com/jobs/"
                      r"([a-z0-9]+)"},
    "listing": {
        "url": "https://{slug}.hire.trakstar.com/",
        "params": {"p": "$page"},
        # The page's script names the board's count: `total_results: '624'`.
        "pager": {"kind": "page", "size": 25, "start": 1, "pages": 40,
                  "total": {"of": {"of": "page",
                                   "transform": r"group:total_results:\s+'(\d+)'"},
                            "transform": "int"}},
        "decoder": {"kind": "html", "select": ".js-careers-page-job-list-item > a",
                    "context": ["div"],
                    "cells": {"title": ".js-job-list-opening-name",
                              "place": ".js-job-list-opening-loc",
                              "city": ".meta-job-location-city",
                              "state": ".meta-job-location-state",
                              "country": ".meta-job-location-country",
                              "dept": ".rb-text-4:not(.js-job-list-opening-meta)",
                              "meta": ".js-job-list-opening-meta"}},
        "fields": {
            "_jid": {"of": "url", "transform": r"group:/jobs/([a-z0-9]+)"},
            "id": {"format": "recruiterbox_{slug}_{_jid}", "when": {"truthy": "_jid"}},
            "title": "title",
            "url": "url",
            # The spans hold "City", "State" and "Country"; a free-text place has none.
            "location": {"first": [{"join": ["city", "state", "country"], "sep": ", "},
                                   {"of": "place", "transform": "one_line"}]},
            "remote_hint": {"const": "recruiterbox:remote",
                            "when": {"contains": ["meta", "fully remote"]}},
            "department": "dept",
        },
    },
    # The listing names no body; the posting page does. Its JSON-LD
    # holds raw line breaks in a string (not valid JSON), so the body is
    # read off the page. A pulled posting's page answers 404.
    "detail": {
        "url": "https://{slug}.hire.trakstar.com/jobs/{jid}/",
        "decoder": {"kind": "html", "select": "body",
                    "cells": {"description": "div.jobdesciption"}},
        "fields": {"description": "description"},
    },
}
