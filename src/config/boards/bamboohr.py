"""The `bamboohr` board spec."""

from __future__ import annotations

from src.rows import JSON

# A posting the board marks remote, by flag or by location type.
REMOTE: JSON = {"any": [{"truthy": "isRemote"}, {"eq": ["locationType", "1"]}]}

SPEC: dict[str, JSON] = {
    "detect": [{"host": "bamboohr.com", "re": [r"(?i)([a-z0-9-]+)\.bamboohr\.com"]}],
    "canary": {"name": "EMS Biomedical", "handle": "ems"},
    "discovery": {"search": [[8, "*.bamboohr.com/careers"]], "hint": [[7, "bamboohr"]]},
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
                 "when": {"all": [REMOTE,
                                  {"truthy": "_loc"}]}},
                {"const": "Remote",
                 "when": REMOTE},
                "_loc"], "default": "Unknown"},
            "remote_hint": {"const": "bamboohr:locationType",
                            "when": REMOTE},
            "department": "departmentLabel",
        },
    },
    "detail": {
        "url": "https://{slug}.bamboohr.com/careers/{jid}/detail",
        "record": "result.jobOpening",
        "fields": {"description": {"of": "description", "transform": "html_text"}},
    },
}
