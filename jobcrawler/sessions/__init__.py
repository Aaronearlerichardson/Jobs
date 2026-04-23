"""Gated-site session capture + reuse (Playwright)."""

from dataclasses import dataclass


@dataclass
class RuntimeConfig:
    """CLI flags for the browser layer. Mutated by discover.py main()."""
    browser_choice: str = "chrome"     # "chrome" | "chromium" | "firefox"
    use_profile: bool = False
    user_data_dir: str | None = None
    profile_directory: str = "Default"
    refresh_profile: bool = False


runtime = RuntimeConfig()


def configure(**kwargs):
    for k, v in kwargs.items():
        if hasattr(runtime, k):
            setattr(runtime, k, v)

    # Persistent contexts only work with the chromium engine
    if runtime.use_profile:
        runtime.browser_choice = "chrome"


from .capture import (   # noqa: E402 — after runtime is defined
    capture_session,
    fetch_gated,
    list_sessions,
    test_session,
)
from .credentials import (   # noqa: E402
    check_credentials,
    get_credentials,
    init_credentials_template,
    load_credentials,
)

__all__ = [
    "capture_session",
    "check_credentials",
    "configure",
    "fetch_gated",
    "get_credentials",
    "init_credentials_template",
    "list_sessions",
    "load_credentials",
    "runtime",
    "test_session",
]
