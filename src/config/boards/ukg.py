"""The `ukg` board spec."""

from __future__ import annotations

from src.rows import JSON

# UKG Pro's other hosts: the board URL shape is the ultipro spec's.
SPEC: dict[str, JSON] = {"detect": [{"host": "ultipro.com", "re": [r"(?i)([a-z0-9-]+\.ultipro\.com)"]}]}
