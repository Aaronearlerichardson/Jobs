"""Discovery pipeline — ask Claude for employers, probe each ATS slug."""

from .apply import apply_to_config
from .pipeline import discover, print_summary, write_discovery_report

__all__ = [
    "apply_to_config",
    "discover",
    "print_summary",
    "write_discovery_report",
]
