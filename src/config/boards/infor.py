"""The `infor` board spec.

Notes:
    A board URL without the org id names no board; the /hcm/Jobs path is
    required because the same hosts serve the signed-in employee app. The
    next-page URL carries opaque record keys, so it is followed verbatim.
    A pulled posting answers 200 either way: record gone, or kept with a
    past posting-end date (2026-09-21, live).
"""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    # A board URL without the org id names no board. The /hcm/Jobs path
    # is required: the same hosts serve the signed-in employee app.
    "detect": [{"host": "inforcloudsuite.com",
                "re": [r"(?i)([a-z0-9-]+\.inforcloudsuite\.com)/hcm/Jobs\b",
                       r"(?i)csk\.HROrganization=([A-Za-z0-9_-]+)"],
                "transform": ["lower", "keep"]}],
    "canary": {"name": "UNC Health", "handle": "css-unchealthunc-prd.inforcloudsuite.com|9999"},
    "handle": {"parts": ["host", "org"]},
    # The posting key is a triple (org, requisition, posting revision),
    # URL-encoded into the path; the parts are named after the listing
    # keys the row id reads.
    "job_ref": {"re": r"(?i)^https?://([^/]+)/hcm/Jobs/form/JobPosting%5BJobPostingSet%5D"
                      r"%28(\d+)%2C(\d+)%2C(\d+)%29\.JobPostingDisplay",
                "parts": ["host", "org", "JobRequisition", "JobPosting"]},
    "listing": {
        "url": "https://{host}/hcm/Jobs/list/JobPosting.SearchForJobsResults",
        "params": {"pageop": "load", "pagesize": "$size",
                   "pagepanel": "JobsHomePage.Jobs.Jobs",
                   "csk.JobBoard": "EXTERNAL", "csk.HROrganization": "{org}"},
        # Every field is wrapped: {"value": ..., "size": ..., ...}.
        "decoder": {"entries": "dataViewSet.data[].fields", "values": "value"},
        # The next-page URL carries opaque record keys: rebuilt by hand
        # it silently re-serves page 1, so it is followed verbatim.
        "pager": {"kind": "cursor", "size": 500, "pages": 40,
                  "next": "dataViewSet.pagingUrls.nextPageUrl",
                  "has_next": "dataViewSet.pagingInfo.hasNext"},
        "fields": {
            "_req": {"first": ["JobRequisition", "JobId"]},
            "_tenant": {"format": "{host}", "transform": "host_label"},
            "id": {"format": "infor_{_tenant}_{_req}_{JobPosting}"},
            "title": "Description",
            "url": {"format": "https://{host}/hcm/Jobs/form/JobPosting%5BJobPostingSet%5D"
                              "%28{org}%2C{_req}%2C{JobPosting}%29.JobPostingDisplay"
                              "?pagesize=1&csk.JobBoard=EXTERNAL&csk.HROrganization={org}"},
            "location": {"of": {"first": ["LocationOfJobDescriptionForSort", "LocationOfJob"]},
                         "transform": "colon_location"},
            "posted_at": {"of": "PostingDateRange_prd_Begin", "transform": "ymd"},
            "department": {"first": ["_op_Category_prd_Description_spc_translation_cp_",
                                     "Category"]},
        },
    },
    "detail": {
        "url": "https://{host}/hcm/Jobs/form/JobPosting%5BJobPostingSet%5D"
               "%28{org}%2C{JobRequisition}%2C{JobPosting}%29.JobPostingDisplay"
               "?pageop=load&pagesize=1&dependentForm=true"
               "&csk.JobBoard=EXTERNAL&csk.HROrganization={org}",
        "decoder": {"values": "value"},
        "fields": {
            "description": {"of": "fields._op_PositionDescription_spc_translation_cp_",
                            "transform": "html_text"},
            # "US:NC:Morrisville | <category> | <work type>"
            "location": {"of": {"of": "fields._op_JobRequisitionLocationCategoryWorkType"
                                      "_spc_translation_cp_", "transform": "before:|"},
                         "transform": "colon_location"},
        },
        "location": "if_unknown",
    },
    # A pulled posting answers 200 either way: its record gone, or kept
    # with a posting-end date in the past (2026-09-21, live).
    "closure": {"closed": [
        {"when": {"any": [{"eq": ["status", "DOES_NOT_EXIST"]}, {"eq": ["statusCode", 404]}]},
         "why": {"const": "posting record gone"}},
        {"when": {"past": {"of": "fields.PostingDateRange_prd_End", "transform": "ymd"}},
         "why": {"join": [{"const": "posting ended"},
                          {"of": "fields.PostingDateRange_prd_End", "transform": "ymd"}]}}],
                "open": {"truthy": "fields"}},
}
