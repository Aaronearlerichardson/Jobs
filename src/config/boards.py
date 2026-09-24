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
    "smartrecruiters": {
        # Not in the lightweight sweep: boards run to thousands of rows.
        "job_ref": {"re": r"smartrecruiters\.com/([A-Za-z0-9_.-]+)/(\d+)", "parts": ["slug", "id"]},
        "listing": {
            "url": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            "params": {"limit": "$size", "offset": "$offset"},
            "decoder": {"kind": "json", "entries": "content"},
            "pager": {"kind": "offset", "size": 100, "pages": 10, "total": "totalFound"},
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
            "location": "never",
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
        # Not in the lightweight sweep: boards run to thousands of rows and
        # are pulled scoped to the locality.
        "handle": {"columns": ["wd_tenant", "wd_pod", "wd_site"],
                   "parts": ["tenant", "pod", "site"],
                   # The CXS path names the tenant's internal id: for a
                   # hyphenated host usually the underscore form (the
                   # hyphen form 422s).
                   "try": {"cxs_tenant": ["{tenant}", "{tenant|underscore}"]},
                   "accept": {"status": [200], "total": True}},
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
            "decoder": {"kind": "json", "entries": "jobPostings"},
            # Only page 0 reports the total. The API serves 2000 rows at
            # most and reports a bigger board as 2000.
            "pager": {"kind": "offset", "size": 20, "pages": 60, "total": "total",
                      "ceiling": 2000},
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
        # A multi-site posting lists as "<N> Locations"; its path names one site.
        "rescue": {"unknown": r"(?i)^\s*\d+\s+locations?\s*$", "cap": 150, "cache_days": 3,
                   "free": {"of": {"of": "externalPath", "transform": "group:^/job/([^/]+)/"},
                            "transform": "dash_space"}},
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
        # A pulled posting's record answers 200 without a title or a body.
        "closure": {"open": {"any": [{"truthy": "jobDescription"}, {"truthy": "title"}]},
                    "unmatched": "no posting record"},
    },
    "infor": {
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
            "decoder": {"kind": "json", "entries": "dataViewSet.data[].fields", "values": "value"},
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
            "decoder": {"kind": "json", "values": "value"},
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
    "phenom": {
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
            # The row order is unstable between requests: half-page overlap
            # catches a row shifting across a page boundary. The server caps
            # size at 500.
            "pager": {"kind": "overlap", "size": 500, "step": 250, "pages": 40,
                      "total": "eagerLoadRefineSearch.totalHits"},
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
}
