"""The `ultipro` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "ultipro.com",
                "re": [r"(?i)recruiting2?\.ultipro\.com/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F\-]{36})"]}],
    "canary": {"name": "Baylor Genetics",
               "handle": "BAY1006BML|0669eed3-5441-4f8e-a7b1-c5df596a4dfe"},
    "sweep": True,
    "prunable": True,
    "handle": {"parts": ["code", "guid"],
               "try": {"host": ["recruiting2", "recruiting"]},
               "accept": {"status_not": [404]},
               "why": "a board answers on one of two hosts and the other 404s, 2026-09"},
    "listing": {
        "method": "POST",
        "url": "https://{host}.ultipro.com/{code}/JobBoard/{guid}/JobBoardView/LoadSearchResults",
        "json": {"opportunitySearch": {"Top": "$size", "Skip": "$offset", "QueryString": "",
                                       "OrderBy": [], "Filters": []}},
        "decoder": {"entries": "opportunities"},
        "pager": {"kind": "offset", "size": 100, "total": "totalCount"},
        "fields": {
            "id": {"format": "ultipro_{code}_{Id:12}"},
            "title": "Title",
            "url": {"format": "https://{host}.ultipro.com/{code}/JobBoard/{guid}"
                              "/OpportunityDetail?opportunityId={Id}"},
            "location": {"first": [
                {"join": ["Locations[0].Address.City",
                          {"first": ["Locations[0].Address.State.Code",
                                     "Locations[0].Address.State"]}], "sep": ", "},
                "Locations[0].LocalizedName"]},
            "description": {"of": "BriefDescription", "transform": "html_text"},
            "department": "JobCategoryName",
        },
    },
}
