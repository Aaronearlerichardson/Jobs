"""The `adp` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    # The host names no board: the two ids ride in the query string.
    "detect": [{"host": "workforcenow.adp.com",
                "re": [r"(?i)workforcenow\.adp\.com", r"(?i)[?&]cid=([0-9a-f-]{8,})",
                       r"(?i)[?&]ccid=([0-9A-Za-z_]+)"]}],
    "canary": {"name": "TARGAN Inc.",
               "handle": "9a6de238-e301-469b-8a29-d35b7eaeebd9|19000101_000001"},
    "sweep": True,
    "eager": True,
    "handle": {"parts": ["cid", "ccid"]},
    "job_ref": {"re": r"(?i)workforcenow\.adp\.com/.*?[?&]cid=([^&]+)&ccId=([^&]+)&jobId=([^&]+)",
                "parts": ["cid", "ccid", "jid"]},
    "listing": {
        "url": "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions",
        "params": {"cid": "{cid}", "ccId": "{ccid}", "locale": "en_US",
                   "$top": "$size", "$skip": "$offset"},
        "decoder": {"entries": "jobRequisitions"},
        # "$skip" counts from 1: 0 reads one row short, and a one-row ask nothing.
        "pager": {"kind": "offset", "size": 50, "start": 1, "total": "meta.totalNumber"},
        "fields": {
            "id": {"format": "adp_{cid:8}_{itemID}"},
            "title": "requisitionTitle",
            "url": {"format": "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
                              "?cid={cid}&ccId={ccid}&jobId={itemID}&lang=en_US"},
            "location": {"join": ["requisitionLocations[].nameCode.shortName"], "sep": "; ",
                         "default": "Unknown"},
            "department": {"join": ["organizationalUnits[].nameCode.shortName"]},
        },
    },
    "detail": {
        "url": "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions/{jid}",
        "params": {"cid": "{cid}", "ccId": "{ccid}", "locale": "en_US"},
        "record": ["jobRequisitions[0]", ""],
        "fields": {"description": {"first": ["requisitionDescription", "description"],
                                   "transform": "html_text"}},
    },
    # A pulled requisition answers 200 with an empty record (2026-09-23).
    "closure": {"closed": {"falsy": "requisitionTitle"},
                "open": {"truthy": "requisitionTitle"}},
}
