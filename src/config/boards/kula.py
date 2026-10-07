"""The `kula` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
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
}
