"""The `avature` board spec.

Avature portals: a path on the tenant's own host, keyed on its URL.

Notes:
    Tenants size their own pages (12, 20); the page's legend gives the
    total. Fetchable since 2026-10.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "avature.net",
                "re": [r"(?i)(https?://[a-z0-9.-]+(?:/[a-z]{2}_[A-Z]{2})?/[a-z]+)"
                       r"/(?:SearchJobs|JobDetail)\b"],
                "careers_url": "{slug}"}],
    "canary": {"name": "Unifi", "handle": "https://careers.unifiservice.com/careers"},
    "handle": {"columns": ["careers_url"], "parts": ["base"]},
    "job_ref": {"re": r"(?i)^(https?://[^/?#]+(?:/[a-z]{2}_[A-Z]{2})?/[a-z]+)/JobDetail/"
                      r"(?:[^/?#]*/)?(\d+)",
                "parts": ["base", "jid"]},
    "listing": {
        "url": "{base|rstrip_slash}/SearchJobs",
        "params": {"jobOffset": "$offset"},
        # The tenant sizes its pages (12, 20); the page's legend says
        # "1-20 of 720 results".
        "pager": {"kind": "offset", "pages": 60,
                  "total": {"of": {"of": "page", "transform": r"group:(?s)\bof\s+([\d,]+)\s+results"},
                            "transform": "int"}},
        "decoder": {"kind": "html", "select": "article.article--result .article__header__text__title a",
                    "context": ["article"],
                    "cells": {"loc": ".list-item-location", "country": ".list-item-country",
                              "dept": ".list-item-department"}},
        "fields": {
            "_jid": {"of": "url", "transform": r"group:/(\d+)/?(?:[?#]|$)"},
            "_key": {"format": "{base}", "transform": "host_key"},
            "id": {"format": "avature_{_key}_{_jid}", "when": {"truthy": "_jid"}},
            "title": "text",
            "url": "url",
            "location": {"first": ["loc", "country"]},
            "department": "dept",
        },
    },
    # A list naming only a country is placed from its title, else its page.
    "rescue": {"when": "always", "unknown": "^[^,]*$", "cap": 150, "cache_days": 7,
               "free": "text",
               "why": "a tenant's list names a country, its posting page the city, 2026-10"},
    "eager": True,
    "detail": {
        "url": "{base|rstrip_slash}/JobDetail/-/{jid}",
        "decoder": {"kind": "html", "select": "body",
                    "cells": {"city": ".article__content__view__field:contains('City') "
                                      ".article__content__view__field__value",
                              "state": ".article__content__view__field:contains('State') "
                                       ".article__content__view__field__value",
                              "description": "article:contains('Description') "
                                             ".article__content__view__field__value"}},
        "fields": {"description": "description",
                   "location": {"join": ["city", "state"], "sep": ", "}},
        "location": "if_unknown",
    },
    "closure": {"via": "page"},
}
