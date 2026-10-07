"""The `taleo_enterprise` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    "detect": [{"host": "taleo.net", "re": [r"(?i)([a-z0-9-]+\.taleo\.net)"],
                "blocklist": ["tbe.taleo.net"]}]}
