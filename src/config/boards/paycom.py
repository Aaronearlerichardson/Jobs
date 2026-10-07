"""The `paycom` board spec."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {"detect": [{"host": "paycomonline.net",
                       "re": [r"(?i)(paycomonline\.net/[A-Za-z0-9/_-]+)"]}]}
