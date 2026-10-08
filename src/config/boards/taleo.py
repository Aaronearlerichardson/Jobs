"""The `taleo` board spec.

Taleo Business Edition: an org on a site path, career center the (org, cws) pair.

Notes:
    Enterprise Taleo stays a lead. Rows 10 a page; later pages read the
    session the first opens.
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "tbe.taleo.net",
                "re": [r"(?i)([a-z0-9-]+\.tbe\.taleo\.net/[a-z0-9]+)/ats/careers/v2/"
                       r"(?:searchResults|viewRequisition)\?org=([A-Za-z0-9_-]+)&cws=(\d+)"]}],
    "canary": {"name": "Nurses and More, Inc.", "handle": "phh.tbe.taleo.net/phh04|NFINDY|37"},
    "eager": True,
    "handle": {"parts": ["site", "org", "cws"]},
    "job_ref": {"re": r"(?i)^https?://([a-z0-9-]+\.tbe\.taleo\.net/[a-z0-9]+)/ats/careers/v2/"
                      r"viewRequisition\?org=([A-Za-z0-9_-]+)&cws=(\d+)&rid=(\d+)",
                "parts": ["site", "org", "cws", "jid"]},
    "listing": {
        "url": "https://{site}/ats/careers/v2/searchResults",
        "params": {"org": "{org}", "cws": "{cws}", "next": "$page", "rowFrom": "$offset"},
        # Rows 10 a page, to the first empty one; a later page reads the
        # session the first one opens.
        "pager": {"kind": "page", "size": 10, "pages": 40, "bare_first": True,
                  "why": "the first request opens the search session, a later one asks "
                         "`next`, 2026-10"},
        "decoder": {"kind": "html", "select": "a.viewJobLink", "context": ["div"],
                    "cells": {"place": "h4 + div", "dept": "h4 + div + div"}},
        "fields": {
            "_rid": {"of": "url", "transform": "group:rid=(\\d+)"},
            "id": {"format": "taleo_{org|lower}_{cws}_{_rid}", "when": {"truthy": "_rid"}},
            "title": "text",
            "url": "url",
            "location": {"of": "place", "transform": "one_line"},
            "department": "dept",
        },
    },
    "detail": {
        "url": "https://{site}/ats/careers/v2/viewRequisition",
        "params": {"org": "{org}", "cws": "{cws}", "rid": "{jid}"},
        "decoder": {"kind": "jsonld"},
        "fields": {"description": "description", "posted_at": "posted_at"},
    },
    "closure": {"via": "page"},
}
