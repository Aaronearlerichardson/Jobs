"""Discovery pipeline — ask Claude for employers, probe each ATS slug."""

from .apply import apply_to_config
from .bciwiki import bciwiki_seed_candidates
from .pipeline import (
    discover,
    discover_companies,
    print_summary,
    write_discovery_report,
)

__all__ = [
    "apply_to_config",
    "bciwiki_seed_candidates",
    "discover",
    "discover_companies",
    "print_summary",
    "write_discovery_report",
]
