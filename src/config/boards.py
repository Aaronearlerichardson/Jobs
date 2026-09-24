"""Every per-platform fact about an ATS board, as data.

`BOARDS[ats]` says how a store row names a board, how its listing is read
and mapped to rows, how one posting is read back, and how a posting's
closure is judged. The engine that reads it is `src.ats.board` (its
schema the models in `src.ats.board.spec`, where each key's meaning and
default are declared); outside them, only
tests/test_boards_spec.py's NAMED_PLATFORMS may name a platform in `src/`.

The literal is JSON-compatible on purpose (str, int, float, bool, None,
list, dict; regexes as strings): tests/test_invariants.py pins
`json.loads(json.dumps(BOARDS)) == BOARDS`, so moving it to a JSON file
later is mechanical.

The host lists below it are derived from the specs' `detect` entries, so
the layers under the engine (the store, the careers-page reader, the
closure prober, page capture) know every vendor host without naming one.
"""

import re

BOARDS = {
    "greenhouse": {
        "detect": [{"host": "greenhouse.io",
                    "re": [r"(?i)(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"]}],
        "canary": {"name": "Databricks", "handle": "databricks"},
        "sweep": True,
        "prunable": True,
        "guess": True,
        "job_ref": {"re": r"greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_.-]+)/jobs/(\d+)"},
        "listing": {
            "url": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
            "probe_url": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
            "decoder": {"entries": "jobs"},
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
        },
        "employer": "company_name",
    },
    "lever": {
        "detect": [{"host": "lever.co", "re": [r"(?i)jobs\.lever\.co/([a-z0-9_-]+)"]}],
        "canary": {"name": "Veeva", "handle": "veeva"},
        "sweep": True,
        "prunable": True,
        "guess": True,
        "job_ref": {"re": r"lever\.co/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{20,})"},
        "listing": {
            "url": "https://api.lever.co/v0/postings/{slug}?mode=json",
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
        },
    },
    "ashby": {
        "detect": [{"host": "ashbyhq.com", "re": [r"(?i)jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)"]}],
        "canary": {"name": "Vanta", "handle": "vanta"},
        "sweep": True,
        "prunable": True,
        "guess": True,
        "job_ref": {"re": r"ashbyhq\.com/([A-Za-z0-9_.-]+)/([0-9a-fA-F-]{20,})"},
        "listing": {
            "url": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
            # The posting API says "jobs"; only the embed payload says "jobPostings".
            "decoder": {"entries": ["jobs", "jobPostings"]},
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
        "detect": [{"host": "bamboohr.com", "re": [r"(?i)([a-z0-9-]+)\.bamboohr\.com"]}],
        "canary": {"name": "EMS Biomedical", "handle": "ems"},
        "sweep": True,
        "prunable": True,
        "eager": True,
        "job_ref": {"re": r"(?i)//([a-z0-9-]+)\.bamboohr\.com/careers/(\d+)"},
        "listing": {
            "url": "https://{slug}.bamboohr.com/careers/list",
            "decoder": {"entries": "result"},
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
        },
    },
    "rippling": {
        "detect": [{"host": "rippling.com", "re": [r"(?i)ats\.rippling\.com/([a-z0-9][a-z0-9-]+)/jobs"]}],
        "canary": {"name": "Blackrock Neurotech", "handle": "blackrockneurotech"},
        "sweep": True,
        "eager": True,
        "job_ref": {"re": r"rippling\.com/([^/]+)/jobs/([0-9a-f-]{36})"},
        "listing": {
            "url": "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs",
            "decoder": {"entries": ["", "jobs"]},
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
        },
    },
    "hibob": {
        "detect": [{"host": "hibob.com", "re": [r"(?i)([a-z0-9][a-z0-9-]+)\.careers\.hibob\.com"]}],
        "sweep": True,
        # Every posting's URL is the board's one /jobs page: the stored job
        # id is what names a posting (closure by board membership).
        "job_ref": {"re": r"(?i)//([a-z0-9-]+)\.careers\.hibob\.com/jobs", "parts": ["slug"]},
        "listing": {
            "url": "https://{slug}.careers.hibob.com/api/job-ad",
            # The API 401s without a same-origin Referer.
            "headers": {"Referer": "https://{slug}.careers.hibob.com/"},
            "decoder": {"entries": "jobAdDetails"},
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
        "detect": [{"host": "workable.com",
                    "re": [r"(?i)apply\.workable\.com/(?:api/v\d+/widget/accounts/)?([a-z0-9][a-z0-9_-]*)"]}],
        "canary": {"name": "It Practice", "handle": "practicetek"},
        # Not in the lightweight sweep (it seeds LOCAL); set "sweep" to add it.
        "eager": True,
        # The tenant-path posting URL names both coordinates; the listing's
        # own short link (/j/<shortcode>) names no account.
        "job_ref": {"re": r"(?i)^https?://apply\.workable\.com/([A-Za-z0-9][A-Za-z0-9_-]*)/j/([A-Za-z0-9]+)"},
        "listing": {
            "url": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
            "decoder": {"entries": "jobs"},
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
        },
    },
    "paylocity": {
        # The board URL's name segment after the company GUID is cosmetic.
        "detect": [{"host": "paylocity.com",
                    "re": [r"(?i)recruiting\.paylocity\.com/[Rr]ecruiting/[Jj]obs/All/([0-9a-fA-F-]{36})"]}],
        "canary": {"name": "United Imaging - North America",
                   "handle": "d527ad39-680d-45fa-9178-38a81898aec2"},
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
        },
        # A pulled posting's detail page still answers 200 (2026-09-23).
        "closure": {"via": "page"},
    },
    "ultipro": {
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
                    "Locations[0].LocalizedName"], "default": "Unknown"},
                "description": {"of": "BriefDescription", "transform": "html_text"},
                "department": "JobCategoryName",
            },
        },
    },
    "adp": {
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
            "pager": {"kind": "offset", "size": 50, "total": "meta.totalNumber"},
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
    },
    "smartrecruiters": {
        "detect": [{"host": "smartrecruiters.com",
                    "re": [r"(?i)(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"]},
                   {"host": "smartrecruiters.com",
                    "re": [r"(?i)api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9]+)/"]}],
        "canary": {"name": "Eurofins", "handle": "Eurofins"},
        # Not in the lightweight sweep: boards run to thousands of rows.
        "job_ref": {"re": r"smartrecruiters\.com/([A-Za-z0-9_.-]+)/(\d+)", "parts": ["slug", "id"]},
        "listing": {
            "url": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            "params": {"limit": "$size", "offset": "$offset"},
            "decoder": {"entries": "content"},
            "pager": {"kind": "offset", "size": 100, "total": "totalFound"},
            "fields": {
                "id": {"format": "sr_{slug}_{id}"},
                "title": "name",
                "url": {"format": "https://jobs.smartrecruiters.com/{slug}/{id}"},
                "location": {"join": ["location.city", "location.region", "location.country"],
                             "sep": ", "},
                "posted_at": "releasedDate",
                "department": {"join": ["department.label", "function.label"]},
            },
        },
        "detail": {
            "url": "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}",
            "fields": {"description": {"join": ["jobAd.sections.jobDescription.text",
                                                "jobAd.sections.qualifications.text",
                                                "jobAd.sections.additionalInformation.text"],
                                       "transform": "html_text"}},
        },
        # A pulled posting answers 200 with active=false. A repost answers
        # under its successor's id, which only postingUrl carries: a verdict
        # on the successor, so neither open nor closed for this row.
        "closure": {"closed": [{"when": {"eq": ["active", False]}, "why": {"const": "active=false"}}],
                    "open": [{"when": {"all": [{"eq": ["active", True]},
                                               {"any": [{"falsy": "id"}, {"falsy": "postingUrl"},
                                                        {"contains": ["postingUrl", "$id"]}]}]},
                              "why": {"const": "active"}}]},
        "employer": "company.name",
    },
    "workday": {
        # The CXS API URL first (the tenant appears twice), then any board
        # URL; the site slot after an optional locale, never an API or
        # asset segment.
        "detect": [{"host": "myworkdayjobs.com",
                    "re": [r"(?i)https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com"
                           r"/wday/cxs/[a-z0-9-]+/([A-Za-z0-9_-]+)/"],
                    "transform": ["lower", "int", None]},
                   {"host": "myworkdayjobs.com",
                    "re": [r"(?i)https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com"
                           r"(?:/[a-z]{2}-[A-Z]{2})?/([A-Za-z0-9_-]+)"],
                    "transform": ["lower", "int", None],
                    "blocklist": ["wday", "cxs", "api", "static", "assets", "login"]}],
        "canary": {"name": "ThermoFisher Scientific IT",
                   "handle": "thermofisher|5|ThermoFisherCareers"},
        # Not in the lightweight sweep: boards run to thousands of rows and
        # are pulled scoped to the locality.
        "handle": {"columns": ["wd_tenant", "wd_pod", "wd_site"],
                   "parts": ["tenant", "pod", "site"],
                   "try": {"cxs_tenant": ["{tenant}", "{tenant|underscore}"]},
                   "accept": {"status": [200], "total": True},
                   "why": "a hyphenated tenant's CXS path takes the underscore form; "
                          "the hyphen form 422s, 2026-08"},
        # The URL's site slot can hold a locale; the company row's wins.
        "job_ref": {"re": r"(?i)^https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com"
                          r"(?:/[a-z]{2}(?:-[A-Za-z]{2})?)?/([^/?#]+)(/job/[^?#]*)",
                    "parts": ["tenant", "pod", "site", "path"]},
        "listing": {
            "method": "POST",
            "url": "https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{cxs_tenant}/{site}/jobs",
            "json": {"appliedFacets": "$facets", "searchText": "$search_text",
                     "limit": "$size", "offset": "$offset"},
            "headers": {"Content-Type": "application/json"},
            "decoder": {"entries": "jobPostings"},
            # Only page 0 reports the total.
            "pager": {"kind": "offset", "size": 20, "pages": 60, "total": "total",
                      "ceiling": 2000,
                      "why": "the API serves 2000 rows at most and reports a bigger "
                             "board as 2000, 2026-09"},
            "scope": {"kind": "facets", "facets": "facets", "param": "facetParameter",
                      "param_re": "(?i)location|country|region|city|state",
                      "values": "values", "id": "id", "label": "descriptor"},
            "fields": {
                "_pid": {"first": [{"of": "externalPath", "transform": "group:([^/]*)$"},
                                   {"of": "title", "transform": "stable_id"}]},
                "id": {"format": "wd_{tenant}_{_pid}"},
                "title": "title",
                "url": {"format": "https://{tenant}.wd{pod}.myworkdayjobs.com/en-US/{site}"
                                  "{externalPath}",
                        "when": {"truthy": "externalPath"},
                        "else": {"format": "https://{tenant}.wd{pod}.myworkdayjobs.com"}},
                "location": "locationsText",
                "posted_at": {"first": ["postedOnDate", "postedOn"]},
                # A listing entry carries six keys, none a department (2026-09-23).
                "department": None,
            },
        },
        # A posting's path names one of its sites (`free`).
        "rescue": {"unknown": r"(?i)^\s*\d+\s+locations?\s*$", "cap": 150, "cache_days": 3,
                   "free": {"of": {"of": "externalPath", "transform": "group:^/job/([^/]+)/"},
                            "transform": "dash_space"},
                   "why": 'a multi-site posting lists as "N Locations", naming no place, 2026-08'},
        "detail": {
            "url": "https://{tenant}.wd{pod}.myworkdayjobs.com/wday/cxs/{cxs_tenant}/{site}{path}",
            "record": "jobPostingInfo",
            "fields": {
                "description": {"of": "jobDescription", "transform": "html_text"},
                "location": {"join": ["location", "additionalLocations[]"], "sep": "; "},
                "remote_hint": {"const": "workday:remoteType",
                                "when": {"any": [{"eq": ["remoteType", "Remote"]},
                                                 {"eq": ["remoteType", "Fully Remote"]}]}},
            },
            "location": "if_unknown",
        },
        "closure": {"open": {"any": [{"truthy": "jobDescription"}, {"truthy": "title"}]},
                    "unmatched": "no posting record",
                    "why": "a pulled posting's record answers 200 without a title or a body, "
                           "2026-08"},
    },
    "infor": {
        # A board URL without the org id names no board. The /hcm/Jobs path
        # is required: the same hosts serve the signed-in employee app.
        "detect": [{"host": "inforcloudsuite.com",
                    "re": [r"(?i)([a-z0-9-]+\.inforcloudsuite\.com)/hcm/Jobs\b",
                           r"(?i)csk\.HROrganization=([A-Za-z0-9_-]+)"],
                    "transform": ["lower", None]}],
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
    },
    "jazzhr": {
        "detect": [{"host": "applytojob.com", "re": [r"(?i)([a-z0-9-]+)\.applytojob\.com"]}],
        "canary": {"name": "Cyclotron Research Centre", "handle": "cyclotroninc"},
        "sweep": True,
        "job_ref": {"re": r"(?i)^(https?://([a-z0-9-]+)\.applytojob\.com/apply/([A-Za-z0-9]+)[^?#]*)",
                    "parts": ["link", "slug", "jid"]},
        "listing": {
            "url": "https://{slug}.applytojob.com/",
            "decoder": {"kind": "html", "select": "a[href*='/apply/']"},
            "fields": {
                "_path": {"of": "href", "transform": "group:(/apply/[A-Za-z0-9]+/[A-Za-z0-9_-]+)"},
                "id": {"format": "{_path}"},
                "url": {"format": "https://{slug}.applytojob.com{_path}"},
                "department": None,
            },
        },
        # Every row is its posting page's JSON-LD, 60 pages a pull.
        "rescue": {"when": "always", "unknown": "^$", "cap": 60,
                   "fields": ["id", "title", "url", "location", "description",
                              "posted_at", "remote_hint"],
                   "why": "the index links each posting's page and names nothing else, 2026-06"},
        "detail": {
            "url": "{link}",
            "decoder": {"kind": "jsonld"},
            "fields": {
                "id": {"format": "jsonld_{slug}_{key}"},
                "title": "title",
                "url": "url",
                "location": {"of": "location", "default": "Unknown"},
                "description": "description",
                "posted_at": "posted_at",
                "remote_hint": {"const": "jsonld:telecommute", "when": {"truthy": "telecommute"}},
            },
            "location": "if_unknown",
        },
        "closure": {"url": "https://{slug}.applytojob.com/apply/{jid}",
                    "why": "a pulled posting's page still answers 200, its slug-free apply "
                           "URL 410, 2026-09"},
    },
    "jobvite": {
        "detect": [{"host": "jobvite.com", "re": [r"(?i)jobs\.jobvite\.com/([a-z0-9][a-z0-9_-]*)"]}],
        "canary": {"name": "Neogenomics", "handle": "neogenomics"},
        "sweep": True,
        "eager": True,
        "handle": {"parts": ["tenant"]},
        "job_ref": {"re": r"(?i)//jobs\.jobvite\.com/([a-z0-9][a-z0-9_-]*)/job/([A-Za-z0-9]+)",
                    "parts": ["tenant", "jid"]},
        "listing": [
            {
                "url": "https://jobs.jobvite.com/{tenant|lower}/search",
                "params": {"p": "$page"},
                "decoder": {"kind": "html",
                            "select": "a.jv-job-list-name[href*='/{tenant}/job/' i]",
                            "context": ["li"], "cells": {"location": ".jv-job-list-location"}},
                "pager": {"kind": "page", "size": 50, "pages": 20},
                "fields": {
                    "_tenant": {"format": "{tenant}", "transform": "lower"},
                    "_jid": {"of": "href", "transform": "group:(?i)/job/([A-Za-z0-9]+)"},
                    "id": {"format": "jv_{_tenant}_{_jid}"},
                    "title": "text",
                    "url": {"format": "https://jobs.jobvite.com/{_tenant}/job/{_jid}"},
                    "location": "location",
                    "department": None,
                },
            },
            # Every row on one page; some tenants replace it with a landing
            # page listing nothing, which is why the search goes first.
            {"url": "https://jobs.jobvite.com/{tenant|lower}/jobs", "params": None, "pager": None,
             "why": "a tenant whose search lists nothing may list every row here, "
                    "2026-09 (inferred)"},
        ],
        "detail": {
            "url": "https://jobs.jobvite.com/{tenant}/job/{jid}",
            "decoder": {"kind": "jsonld"},
            "fields": {
                "description": {"of": "description", "transform": "one_line"},
                "location": "location",
                "posted_at": "posted_at",
                "remote_hint": {"const": "jsonld:telecommute", "when": {"truthy": "telecommute"}},
            },
            "location": "if_unknown",
        },
        "closure": {"via": "page"},
    },
    "kula": {
        "detect": [{"host": "kula.ai", "re": [r"(?i)careers\.kula\.ai/([a-z0-9_-]+)"]}],
        "canary": {"name": "Precision Neuroscience", "handle": "precision-neuroscience"},
        "sweep": True,
        "listing": {
            "url": "https://careers.kula.ai/{slug}",
            # A row is an anchor and the nearest block around it holding two
            # lines of text: department, title, location.
            "decoder": {"kind": "html", "select": "a[href*='/{slug}/']", "context": "lines",
                        "base": "https://careers.kula.ai"},
            "fields": {
                "_n": {"of": "url", "transform": "group:/(\\d+)/?$"},
                "id": {"format": "kula_{slug}_{_n}"},
                "title": {"first": ["lines[1]", "lines[0]"], "default": "Unknown"},
                "url": "url",
                "location": {"of": "lines[2]", "transform": "before:;", "default": "See posting"},
                "department": {"of": "lines[0]", "when": {"truthy": "lines[1]"}},
            },
        },
    },
    "successfactors": {
        "detect": [{"host": "successfactors.",
                    "re": [r"(?i)([a-z0-9-]+)\.(?:successfactors|sapsf)\.(?:com|eu)"]},
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
            # wrap back to earlier rows instead of running dry.
            "pager": {"kind": "offset", "size": 25, "pages": 80,
                      "total": {"of": {"of": "page", "transform": "group:(?s)class=\"paginationLabel\""
                                                                  "[^>]*>.*?of\\s*<b>\\s*([\\d,]+)\\s*</b>"},
                                "transform": "int"}},
            "fields": {
                # The /job/ slug can lead with "<City>,-<ST>-", spaces as hyphens.
                "_path": {"of": "url", "transform": "unquote"},
                "_city": {"of": "_path", "transform": "group:/job/(.+?),-[A-Z]{2}-"},
                "_state": {"of": "_path", "transform": "group:/job/.+?,-([A-Z]{2})-"},
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
                    {"format": "{_city|dash_space}, {_state}", "when": {"truthy": "_city"}},
                    {"of": {"first": ["cell", {"of": "context", "transform": "snippet"}]},
                     "transform": "cut_date_tail"}]},
                "department": None,
            },
        },
    },
    "icims": {
        "detect": [{"host": "icims.com", "re": [r"(?i)([a-z0-9-]+)\.icims\.com"]}],
        "canary": {"name": "FUJIFILM Healthcare Americas Corporation",
                   "handle": "uscareers-fujifilm"},
        # A row naming no place is kept by a location filter its title passes.
        "unlocated": "title",
        "job_ref": {"re": r"(?i)^(https?://[a-z0-9-]+\.icims\.com/jobs/\d+/[^?#]*)",
                    "parts": ["link"]},
        "listing": [
            {
                "url": "https://{slug}.icims.com/jobs/search?ss=1&in_iframe=1",
                "params": {"pr": "$page"},
                # The WAF 405s a Chrome UA arriving without Chrome's client
                # hints; a bare platform UA passes.
                "headers": {"User-Agent": "$plain_user_agent"},
                # The selector also finds the search shell's own links
                # (/jobs/intro, /jobs/login, the pager's), which name no posting.
                "decoder": {"kind": "html", "select": "a.iCIMS_Anchor, a[href*='/jobs/']",
                            "context": "parent"},
                # Tenants serve 20 or 50 a page.
                "pager": {"kind": "page", "pages": 8, "bare_first": True,
                          "why": "the first search page takes no page number, 2026-08"},
                # A free-text place term some tenants answer with "No Results
                # Found": the whole board is read then.
                "scope": {"kind": "param", "params": {"searchLocation": "$locality_abbr"},
                          "located": "$locality_abbr"},
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
                    "location": {"of": "context", "transform": "loc_text"},
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
    },
    "peopleadmin": {
        # Only the hosted tenants carry a signature (a university serving
        # the software from its own hostname is added by hand); the board
        # is the tenant's origin.
        "detect": [{"host": "peopleadmin.com", "re": [r"(?i)([a-z0-9-]+)\.peopleadmin\.com"],
                    "careers_url": "https://{slug}.peopleadmin.com"}],
        "canary": {"name": "UNC Chapel Hill", "handle": "unc.peopleadmin.com"},
        # The board is the tenant's host, keyed on any URL on it.
        "handle": {"columns": ["careers_url"], "parts": ["base"]},
        # A tenant is one campus: a posting naming no place is on it.
        "unlocated": "keep",
        "listing": [
            {
                "url": "https://{base|host}/postings/all_jobs.atom",
                "headers": {"Accept": "application/atom+xml"},
                "decoder": {"kind": "atom"},
                "fields": {
                    "_url": {"first": ["link@href", "id"]},
                    "_key": {"of": {"format": "{base|host}"}, "transform": "host_key"},
                    "_pid": {"first": [{"of": "_url", "transform": "group:/postings/(\\d+)"},
                                       {"of": "_url", "transform": "stable_id"}]},
                    "_title": {"of": "title", "transform": "one_line"},
                    "id": {"format": "pa_{_key}_{_pid}"},
                    "title": "_title",
                    "url": "_url",
                    # The place the title names, else the campus the feed's own
                    # title names; a posting's body is not read for one.
                    "location": {"first": [{"of": "_title", "transform": "place"},
                                           {"of": "feed.title", "transform": "place"}]},
                    "description": {"join": ["author.name",
                                             {"of": {"first": ["content", "summary"]},
                                              "transform": "html_text"}],
                                    "sep": " | "},
                    "posted_at": {"first": ["published", "updated"]},
                    # The hiring department ("Epidemiology - 463501").
                    "department": "author.name",
                },
            },
            {"url": "https://{base|host}/postings/search.atom",
             "why": "a tenant whose whole-board feed lists nothing may serve its "
                    "default saved search, 2026-09 (inferred)"},
        ],
        # No detail: a posting's page is under the host's robots disallow.
    },
    "custom": {
        "canary": {"name": "Microsoft", "handle": "https://microsoft.ai/careers/"},
        "sweep": True,
        # A self-hosted careers page, read by the careers-page reader
        # (src.ats.board.custom).
        "handle": {"columns": ["careers_url"], "parts": ["page"]},
        "listing": {
            "url": "{page}",
            "decoder": {"kind": "html", "select": "$job_links"},
            "fields": {
                "_key": {"of": "url", "transform": "url_key:48"},
                "id": {"format": "custom_{_key}"},
                "title": "title",
                "url": "url",
                "location": "location",
                "department": None,
            },
        },
    },
    "wpjson": {
        "canary": {"name": "restor3d", "handle": "https://www.restor3d.com/company/careers/"},
        "sweep": True,
        # A WordPress theme's careers route, keyed on any page of the site.
        "handle": {"columns": ["careers_url"], "parts": ["site"]},
        "listing": {
            "url": "{site|origin}/wp-json/post-filters-archive/get-posts",
            "params": {"post_type": "career", "posts_per_page": "$size", "paged": "$page"},
            "decoder": {"entries": "posts"},
            # Every page declares the last.
            "pager": {"kind": "page", "size": 100, "pages": 50, "start": 1,
                      "declared": {"of": "max_num_pages", "transform": "int", "default": 1}},
            "fields": {
                "_site": {"format": "{site|host_nowww}"},
                "id": {"format": "wpjson_{_site}_{ID}"},
                "title": {"of": "post_title", "transform": "one_line", "default": "Unknown"},
                "url": {"first": ["link.url", "permalink"]},
                "location": {"join": [{"of": "location.city", "transform": "one_line"},
                                      {"of": "location.state", "transform": "one_line"}],
                             "sep": ", ", "default": "See posting"},
                "posted_at": "post_date",
                "department": None,
            },
        },
        # A posting's URL is its outbound apply page on the applicant
        # portal's own host, so no job_ref: the detail reads the row's URL.
        "detail": {
            "url": "{url}",
            "decoder": {"kind": "html",
                        "select": ["#portalViewRequirement", "[class*='bmportalrequirementdetails']"]},
            "fields": {"description": "text"},
        },
        "closure": {"via": "page"},
    },
    "phenom": {
        # The tenant's own site is the board, so no vendor host names it:
        # every page embeds its widget API origin, the handle.
        "detect": [{"re": [r'(?i)"widgetApiEndpoint"\s*:\s*"https?://([a-z0-9.-]+)/widgets"']}],
        "canary": {"name": "PPD", "handle": "jobs.thermofisher.com"},
        # The listing lives under a locale prefix only the board's root
        # redirect names (/us/en, /global/en, ...).
        "handle": {"follow": {"base": "{slug}"}},
        # Last in this table: its URLs are the ones on the tenant's own host.
        "job_ref": {"re": r"^(https?://([^/?#]+)/[a-z]{2,8}/[a-z]{2}(?:[-_][A-Za-z]{2})?)"
                          r"/job/([^/?#]+)/?$",
                    "parts": ["base", "slug", "reqId"]},
        "listing": {
            "url": "{base}/search-results",
            "params": {"from": "$offset", "size": "$size"},
            "decoder": {"kind": "json_in_html", "regex": r"phApp\.ddo\s*=\s*",
                        "entries": "eagerLoadRefineSearch.data.jobs"},
            # The server caps size at 500.
            "pager": {"kind": "overlap", "size": 500, "step": 250, "pages": 40,
                      "total": "eagerLoadRefineSearch.totalHits",
                      "why": "row order shifts between requests, carrying rows across page "
                             "boundaries, 2026-09"},
            "fields": {
                "_req": {"first": ["reqId", "jobId"]},
                "_key": {"format": "{slug}", "transform": "host_key"},
                "id": {"format": "phenom_{_key}_{_req}"},
                "title": "title",
                "url": {"format": "{base}/job/{_req}"},
                "location": {"first": ["location", "cityStateCountry", "cityState",
                                       {"join": ["city", "state", "country"], "sep": ", "}]},
                "posted_at": "postedDate",
                "department": "category",
            },
        },
        "detail": {
            "url": "{base}/job/{reqId}",
            "decoder": {"kind": "json_in_html", "regex": r"phApp\.ddo\s*=\s*"},
            "record": "jobDetail.data.job",
            "fields": {
                "description": {"of": "description", "transform": "html_text"},
                "location": {"first": ["location", "cityStateCountry", "cityState",
                                       {"join": ["city", "state", "country"], "sep": ", "},
                                       {"join": ["standardised_multi_location[]"
                                                 ".standardisedMapQueryLocation"], "sep": "; "}]},
            },
            "location": "always",
        },
        "closure": {"via": "page"},
    },
    # Detection-only platforms: real ATSes discovery recognises but cannot
    # fetch (bot-protected APIs or JS-only boards). A lead's detection
    # names a host or path for the note; an entry with no `re` claims the
    # vendor's host and detects nothing.
    "eightfold": {"detect": [{"host": "eightfold.ai", "re": [r"(?i)([a-z0-9-]+\.eightfold\.ai)"]}]},
    "dayforce": {"detect": [{"host": "dayforcehcm.com",
                             "re": [r"(?i)(dayforcehcm\.com/[a-zA-Z-]+/[a-zA-Z0-9_-]+)"]}]},
    "recruitee": {"detect": [{"host": "recruitee.com", "re": [r"(?i)([a-z0-9-]+\.recruitee\.com)"]}]},
    "teamtailor": {"detect": [{"host": "teamtailor.com",
                               "re": [r"(?i)([a-z0-9-]+\.teamtailor\.com)"]}]},
    "taleo": {"detect": [{"host": "taleo.net", "re": [r"(?i)([a-z0-9-]+\.taleo\.net)"]}]},
    # UKG Pro's other hosts: the board URL shape is the ultipro spec's.
    "ukg": {"detect": [{"host": "ultipro.com", "re": [r"(?i)([a-z0-9-]+\.ultipro\.com)"]}]},
    "paycom": {"detect": [{"host": "paycomonline.net",
                           "re": [r"(?i)(paycomonline\.net/[A-Za-z0-9/_-]+)"]}]},
    "breezy": {"detect": [{"host": "breezy.hr", "re": [r"(?i)([a-z0-9-]+\.breezy\.hr)"]}]},
    "gohire": {"detect": [{"host": "gohire.io", "re": [r"(?i)([a-z0-9-]+\.gohire\.io)"]}]},
    "polymer": {"detect": [{"host": "polymer.co"}]},
    "gusto": {"detect": [{"host": "gusto.com"}]},
}

#: The spec that reads a careers page itself, no ATS signature on it: the
#: sniffer's last resort, a board named by its URL alone.
CAREERS_PAGE_ATS = "custom"

#: The store columns naming a board whose spec sets no `handle.columns`:
#: the default of src.ats.board.spec.Handle, and the store's.
DEFAULT_HANDLE_COLUMNS = ("slug",)

#: Job aggregators: hosts listing other employers' postings. A careers page
#: never names one as its own board, and they bot-gate anonymous reads, so
#: a probe there says nothing.
AGGREGATOR_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.", "ziprecruiter.com",
                    "simplyhired.com", "monster.com", "dice.com", "builtin.com")


def _hosts(fetchable):
    """The vendor hosts the specs' `detect` entries claim, in spec order;
    only a fetchable spec's (one with a `listing`) when `fetchable`."""
    return tuple(dict.fromkeys(d["host"] for s in BOARDS.values()
                               if s.get("listing") or not fetchable
                               for d in s.get("detect", []) if "host" in d))


#: Every ATS vendor host, fetchable or lead: a page there names a board,
#: not the company that owns it.
BOARD_HOSTS = _hosts(fetchable=False)
#: The vendor hosts of the platforms the engine fetches.
FETCHABLE_HOSTS = _hosts(fetchable=True)
#: Hosts shared by many employers: a page there names a board or a listing,
#: never the company that owns it, so it is no company's own careers page
#: or website. Google's serve its search and many employers' Sites pages.
SHARED_HOSTS = AGGREGATOR_HOSTS + BOARD_HOSTS + ("google.com",)


def hosts_re(hosts):
    """A case-blind regex finding any of `hosts` (literal fragments) in a
    URL or host."""
    return re.compile("|".join(map(re.escape, hosts)), re.I)
