"""Discovery pipeline — ask Claude for employers, probe each ATS slug."""

from .pipeline import discover, print_summary, write_discovery_report

__all__ = ["discover", "print_summary", "write_discovery_report"]
