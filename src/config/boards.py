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
    "bamboohr": {
        "sweep": True,
        "prunable": True,
        "eager": True,
        "job_ref": {"re": r"(?i)//([a-z0-9-]+)\.bamboohr\.com/careers/(\d+)",
                    "parts": ["slug", "jid"]},
        "listing": {
            "url": "https://{slug}.bamboohr.com/careers/list",
            "decoder": {"kind": "json", "entries": "result"},
            "fields": {
                "_loc": {"join": ["location.city", "location.state"], "sep": ", "},
                "id": {"format": "bamboo_{slug}_{id}"},
                "title": "jobOpeningName",
                "url": {"format": "https://{slug}.bamboohr.com/careers/{id}"},
                "location": {"first": [
                    {"format": "Remote / {_loc}",
                     "when": {"all": [{"any": [{"truthy": "isRemote"}, {"eq": ["locationType", "1"]}]},
                                      {"truthy": "_loc"}]}},
                    {"const": "Remote",
                     "when": {"any": [{"truthy": "isRemote"}, {"eq": ["locationType", "1"]}]}},
                    "_loc"], "default": "Unknown"},
                "remote_hint": {"const": "bamboohr:locationType",
                                "when": {"any": [{"truthy": "isRemote"},
                                                 {"eq": ["locationType", "1"]}]}},
                "department": "departmentLabel",
            },
        },
        "detail": {
            "url": "https://{slug}.bamboohr.com/careers/{jid}/detail",
            "record": "result.jobOpening",
            "fields": {"description": {"of": "description", "transform": "html_text"}},
            "location": "never",
        },
    },
    "rippling": {
        "sweep": True,
        "eager": True,
        "job_ref": {"re": r"rippling\.com/([^/]+)/jobs/([0-9a-f-]{36})", "parts": ["slug", "jid"]},
        "listing": {
            "url": "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs",
            "decoder": {"kind": "json", "entries": ["", "jobs"]},
            "fields": {
                "id": {"format": "rippling_{slug}_{uuid:12}"},
                "title": "name",
                "url": {"first": ["url", {"format": "https://ats.rippling.com/{slug}/jobs/{uuid}"}]},
                "location": {"first": ["workLocation.label",
                                       {"join": ["workLocations[]"], "sep": ", ", "max": 3}],
                             "default": "Unknown"},
                "department": {"first": ["department.label", "department"]},
            },
        },
        "detail": {
            "url": "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{jid}",
            # `description` is a {role, company} pair of HTML: the role first.
            "fields": {"description": {"first": [{"join": ["description.role", "description.company"]},
                                                 "description"],
                                       "transform": "html_text"}},
            "location": "never",
        },
    },
    "hibob": {
        "sweep": True,
        # Every posting's URL is the board's one /jobs page: the stored job
        # id is what names a posting (closure by board membership).
        "job_ref": {"re": r"(?i)//([a-z0-9-]+)\.careers\.hibob\.com/jobs", "parts": ["slug"]},
        "listing": {
            "url": "https://{slug}.careers.hibob.com/api/job-ad",
            # The API 401s without a same-origin Referer.
            "headers": {"Referer": "https://{slug}.careers.hibob.com/"},
            "decoder": {"kind": "json", "entries": "jobAdDetails"},
            "fields": {
                "id": {"format": "hibob_{slug}_{id:12}"},
                "title": "title",
                "url": {"format": "https://{slug}.careers.hibob.com/jobs"},
                "location": {"join": [{"first": ["site", "country"]}, "workspaceType"],
                             "sep": " - ", "default": "Unknown"},
                "description": {"of": "description", "transform": "html_text"},
                "posted_at": "publishedAt",
                "remote_hint": {"const": "hibob:workspaceType",
                                "when": {"eq": ["workspaceType", "remote"]}},
                "department": "department",
            },
        },
        "closure": {"via": "listing"},
    },
    "workable": {
        # Not in the lightweight sweep (it seeds LOCAL); set "sweep" to add it.
        "eager": True,
        # The tenant-path posting URL names both coordinates; the listing's
        # own short link (/j/<shortcode>) names no account.
        "job_ref": {"re": r"(?i)^https?://apply\.workable\.com/([A-Za-z0-9][A-Za-z0-9_-]*)/j/([A-Za-z0-9]+)",
                    "parts": ["slug", "jid"]},
        "listing": {
            "url": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
            "decoder": {"kind": "json", "entries": "jobs"},
            "fields": {
                "id": {"format": "workable_{slug}_{shortcode}"},
                "title": "title",
                "url": {"format": "https://apply.workable.com/{slug}/j/{shortcode}/"},
                "location": {"first": [
                    {"merge": {"primary": {"join": ["city", "state", "country"], "sep": ", "},
                               "extras": {"each": "locations",
                                          "do": {"join": ["city", "region", "country"], "sep": ", "},
                                          "skip": {"truthy": "hidden"}}}},
                    {"const": "Remote", "when": {"truthy": "telecommuting"}}],
                    "default": "Unknown"},
                "posted_at": {"first": ["published_on", "created_at"]},
                "remote_hint": {"const": "workable:telecommuting",
                                "when": {"eq": ["telecommuting", True]}},
                "department": "department",
            },
        },
        "detail": {
            "url": "https://apply.workable.com/api/v1/accounts/{slug}/jobs/{jid}",
            # `requirements` is the part the fit model reads; `benefits` is
            # per-board boilerplate and stays out.
            "fields": {"description": {"join": ["description", "requirements"], "sep": "\n",
                                       "transform": "html_text"}},
            "location": "never",
        },
    },
    "paylocity": {
        "sweep": True,
        "eager": True,
        "job_ref": {"re": r"(?i)recruiting\.paylocity\.com/Recruiting/Jobs/Details/(\d+)",
                    "parts": ["jid"]},
        "listing": {
            # The slug is the company GUID; the trailing name segment is cosmetic.
            "url": "https://recruiting.paylocity.com/recruiting/jobs/All/{slug}/x",
            "decoder": {"kind": "json_in_html", "regex": r"pageData\s*=\s*", "entries": "Jobs"},
            "fields": {
                "id": {"format": "paylocity_{slug:8}_{JobId}"},
                "title": "JobTitle",
                "url": {"format": "https://recruiting.paylocity.com/Recruiting/Jobs/Details/{JobId}"},
                "location": {"first": ["LocationName",
                                       {"join": ["JobLocation.City", "JobLocation.State"], "sep": ", "},
                                       {"const": "Remote", "when": {"truthy": "IsRemote"}},
                                       "JobLocation.Country"],
                             "default": "Unknown"},
                # No "description": the listing's Description is a teaser cut at
                # ~110 characters (2026-09-23); the body is the detail page.
                "remote_hint": {"const": "paylocity:isRemote", "when": {"truthy": "IsRemote"}},
                "department": "HiringDepartment",
            },
        },
        "detail": {
            "url": "https://recruiting.paylocity.com/Recruiting/Jobs/Details/{jid}",
            "decoder": {"kind": "html", "select": ".job-preview-details, [class*=job-preview]"},
            # The page's "Apply <title> <location> Apply" chrome leads the body.
            "fields": {"description": {"of": "text", "transform": "after_marker:Description"}},
            "location": "never",
        },
        # A pulled posting's detail page still answers 200 (2026-09-23).
        "closure": {"via": "page"},
    },
    "ultipro": {
        "sweep": True,
        "prunable": True,
        "handle": {"parts": ["code", "guid"],
                   # A board answers on one of two hosts; the other 404s.
                   "try": {"host": ["recruiting2", "recruiting"]},
                   "accept": {"status_not": [404]}},
        "listing": {
            "method": "POST",
            "url": "https://{host}.ultipro.com/{code}/JobBoard/{guid}/JobBoardView/LoadSearchResults",
            "json": {"opportunitySearch": {"Top": "$size", "Skip": "$offset", "QueryString": "",
                                           "OrderBy": [], "Filters": []}},
            "decoder": {"kind": "json", "entries": "opportunities"},
            "pager": {"kind": "offset", "size": 100, "pages": 10, "total": "totalCount"},
            "fields": {
                "id": {"format": "ultipro_{code}_{Id:12}"},
                "title": "Title",
                "url": {"format": "https://{host}.ultipro.com/{code}/JobBoard/{guid}"
                                  "/OpportunityDetail?opportunityId={Id}"},
                "location": {"first": [
                    {"join": ["Locations[0].Address.City",
                              {"first": ["Locations[0].Address.State.Code",
                                         "Locations[0].Address.State"]}], "sep": ", "},
                    "Locations[0].LocalizedName"], "default": "Unknown"},
                "description": {"of": "BriefDescription", "transform": "html_text"},
                "department": "JobCategoryName",
            },
        },
        "closure": {"via": "page"},
    },
    "adp": {
        "sweep": True,
        "eager": True,
        "handle": {"parts": ["cid", "ccid"]},
        "job_ref": {"re": r"(?i)workforcenow\.adp\.com/.*?[?&]cid=([^&]+)&ccId=([^&]+)&jobId=([^&]+)",
                    "parts": ["cid", "ccid", "jid"]},
        "listing": {
            "url": "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions",
            "params": {"cid": "{cid}", "ccId": "{ccid}", "locale": "en_US",
                       "$top": "$size", "$skip": "$offset"},
            "decoder": {"kind": "json", "entries": "jobRequisitions"},
            "pager": {"kind": "offset", "size": 50, "pages": 10, "total": "meta.totalNumber"},
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
            "location": "never",
        },
        # A pulled requisition answers 200 with an empty record (2026-09-23).
        "closure": {"via": "detail", "closed": {"falsy": "requisitionTitle"},
                    "open": {"truthy": "requisitionTitle"}},
    },
}
