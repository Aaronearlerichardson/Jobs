"""The `icims` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "icims.com", "re": [r"(?i)([a-z0-9-]+)\.icims\.com"]}],
    "discovery": {"search": [[7, "*.icims.com"]], "hint": [[5, "icims"]]},
    "canary": {"name": "FUJIFILM Healthcare Americas Corporation",
               "handle": "uscareers-fujifilm"},
    "job_ref": {"re": r"(?i)^(https?://[a-z0-9-]+\.icims\.com/jobs/\d+/[^?#]*)",
                "parts": ["link"]},
    "listing": [
        {
            "url": "https://{slug}.icims.com/jobs/search?ss=1&in_iframe=1",
            "params": {"pr": "$page", "searchLocation": "$facets"},
            # The WAF 405s a Chrome UA arriving without Chrome's client
            # hints; a bare platform UA passes.
            "headers": {"User-Agent": "$plain_user_agent"},
            # The selector also finds the search shell's own links
            # (/jobs/intro, /jobs/login, the pager's), which name no posting.
            # A row's location column is a "Location"-labelled header or
            # field, or the map-marked city, state and country fields.
            "decoder": {"kind": "html", "select": "a.iCIMS_Anchor, a[href*='/jobs/']",
                        "context": ["li", "div"], "selects": True,
                        "cells": {
                            "place": ".field-label:contains('Location') + span, "
                                     "dt:contains('Location') + dd",
                            "city": "dt:has(.glyphicons-map-marker):contains('City') + dd",
                            "state": "dt:has(.glyphicons-map-marker):contains('State') + dd",
                            "country": "dt:has(.glyphicons-map-marker):contains('Country')"
                                       " + dd"}},
            # Tenants serve 20 or 50 a page.
            "pager": {"kind": "page", "pages": 8, "bare_first": True,
                      "why": "the first search page takes no page number, 2026-08"},
            # The search form's location options ("12781-12817-Durham")
            # whose label names the area, asked together; a tenant
            # offering none is read whole.
            "scope": {"kind": "facets", "facets": "selects", "param": "name",
                      "param_re": "^searchLocation$", "values": "options", "id": "value",
                      "label": "label"},
            "fields": {
                "_jid": {"of": "href", "transform": "group:/jobs/(\\d+)/"},
                # The posting's own host names the tenant: a board kept under a
                # portal alias lists postings on the tenant's host.
                "_tenant": {"first": [
                    {"of": {"of": "url", "transform": "group:(?i)^https?://([a-z0-9-]+)\\.icims\\.com"},
                     "transform": "lower"},
                    {"format": "{slug}"}]},
                "_path": {"of": "url", "transform": "group:^([^?#]*)"},
                # A screen-reader label leads the anchor's text.
                "_title": {"of": "raw", "transform": "strip_labels"},
                "id": {"format": "icims_{_tenant}_{_jid}", "when": {"truthy": "_title"}},
                "title": {"of": "_title", "when": {"truthy": "_jid"}},
                # One URL per posting: its path, and the flag selecting the
                # server-rendered document.
                "url": {"format": "{_path}?in_iframe=1", "when": {"truthy": "_jid"}},
                "location": {"first": [{"join": ["city", "state", "country"], "sep": ", "},
                                       "place"]},
                "department": None,
            },
        },
        # Titled by the URL slug; some tenants' WAF 403s it.
        {
            "url": "https://{slug}.icims.com/sitemap.xml",
            "params": None,
            "pager": None,
            "why": "a JS-shell tenant's search page lists nothing, its sitemap every "
                   "live posting, 2026-08",
            "decoder": {"kind": "html", "select": "loc"},
            "fields": {
                "_jid": {"of": "text", "transform": "group:/jobs/(\\d+)/[^/]+/job"},
                "_tenant": {"first": [
                    {"of": {"of": "text", "transform": "group:(?i)^https?://([a-z0-9-]+)\\.icims\\.com"},
                     "transform": "lower"},
                    {"format": "{slug}"}]},
                "_path": {"of": "text", "transform": "group:^([^?#]*)"},
                "_slug": {"of": "text", "transform": "group:/jobs/\\d+/([^/]+)/job"},
                "id": {"format": "icims_{_tenant}_{_jid}"},
                "title": {"of": {"of": "_slug", "transform": "unquote"}, "transform": "dash_space"},
                "url": {"format": "{_path}?in_iframe=1", "when": {"truthy": "_jid"}},
                "department": None,
            },
        },
    ],
    # From its JSON-LD; 150 reads a pull, a found place kept a week.
    "rescue": {"when": "always", "unknown": "^$", "cap": 150, "cache_days": 7,
               "fields": ["location", "description"],
               "why": "the listing seldom names a place; each posting's page does, 2026-08"},
    "detail": {
        "url": "{link}?in_iframe=1",
        "headers": {"User-Agent": "$plain_user_agent"},
        "decoder": {"kind": "jsonld"},
        "fields": {"description": "description", "location": "location"},
        "location": "if_unknown",
    },
    # A pulled posting's page answers 410.
    "closure": {"via": "page"},
}
