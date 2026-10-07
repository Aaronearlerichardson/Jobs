"""The `successfactors` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    # A signature names the vendor's asset host; the board is the site
    # that carried it.
    "detect": [{"host": "successfactors.",
                "re": [r"(?i)([a-z0-9-]+)\.(?:successfactors|sapsf)\.(?:com|eu)"],
                "careers_url": "{page|origin}"},
               {"host": "sapsf."}],
    "canary": {"name": "Duke University", "handle": "https://careers.duke.edu"},
    # The board is the careers site itself, keyed on its URL.
    "handle": {"columns": ["careers_url"], "parts": ["base"]},
    "listing": {
        "url": "{base|rstrip_slash}/search/?startrow={offset}",
        "headers": {"Accept": "text/html"},
        "decoder": {"kind": "html", "select": ["a.jobTitle-link", "a[href*='/job/']"],
                    "context": ["tr", "li", "div"],
                    "cells": {"cell": "[class*='jobLocation']"}, "base": "{base}"},
        # The standard theme's "Results 1 - 25 of 621"; a custom skin
        # may render none. A repeated page ends the walk: some tenants
        # wrap back to earlier rows instead of running dry. No size: a
        # tenant serves 10, 25 or 100 rows a page, its own choice.
        "pager": {"kind": "offset", "pages": 80,
                  "total": {"of": {"of": "page", "transform": "group:(?s)class=\"paginationLabel\""
                                                              "[^>]*>.*?of\\s*<b>\\s*([\\d,]+)\\s*</b>"},
                            "transform": "int"}},
        "fields": {
            # The /job/ slug can lead with "<City>,-<ST>-", spaces as
            # hyphens, just ahead of the title's first word; a ",-XX-"
            # inside a title is no place.
            "_path": {"of": "url", "transform": "unquote"},
            "_city": {"of": "_path", "transform": "group:/job/([^/,]+?),-[A-Z]{2}-"},
            "_state": {"of": "_path", "transform": "group:/job/[^/,]+?,-([A-Z]{2})-"},
            "_word": {"of": "text", "transform": "group:(\\w+)"},
            "_lead": {"format": ",-{_state}-{_word}"},
            "_jid": {"first": [{"of": "url", "transform": "group:/job/[^/]+/(\\d+)"},
                               {"of": "url", "transform": "group:/job/([^/?#]+)"},
                               {"of": "url", "transform": "stable_id"}]},
            "_key": {"format": "{base}", "transform": "alnum_tail:16"},
            "id": {"format": "sf_{_key}_{_jid}", "when": {"truthy": "href"}},
            "title": "text",
            "url": "url",
            # Else the theme's location cell, else a place in the row's
            # text; either can carry a glued-on posting date.
            "location": {"first": [
                {"format": "{_city|dash_space}, {_state}",
                 "when": {"contains": ["_path", "$_lead"]}},
                {"of": {"first": ["cell", {"of": "context", "transform": "snippet"}]},
                 "transform": "cut_date_tail"}]},
            "department": None,
        },
    },
}
