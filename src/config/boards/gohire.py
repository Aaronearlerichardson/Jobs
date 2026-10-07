"""The `gohire` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {"detect": [{"host": "gohire.io", "re": [r"(?i)([a-z0-9-]+\.gohire\.io)"]}]}
