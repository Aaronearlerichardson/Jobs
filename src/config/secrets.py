"""Every environment variable the app reads, as one typed Settings class,
with placeholders for local development. Nothing here reads the profile.

    PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."
    cmd.exe:     set ANTHROPIC_API_KEY=sk-ant-...
    bash/zsh:    export ANTHROPIC_API_KEY=sk-ant-...
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import (BeforeValidator, Field, PositiveInt, ValidationError,
                      model_validator)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """The environment, typed: each field reads the variable of the same
    name in upper case. A blank or whitespace-only value counts as unset,
    and values are trimmed (tests/test_config_env.py).

    Notes:
        Blank-is-unset because an exported-but-empty ANTHROPIC_API_KEY (a
        CI runner, a shell profile clearing it) once read as "a key is
        configured", and the scorers authenticated with nothing instead of
        degrading to their offline fallbacks.
    """
    model_config = SettingsConfigDict(extra="ignore")

    # Digest email is opt-in and OFF until both are set; a blank
    # GMAIL_ADDRESS disables emailing (src/digest/render.py).
    gmail_address: str = ""
    gmail_app_password: str = "YOUR_APP_PASSWORD_HERE"
    anthropic_api_key: str = "YOUR_ANTHROPIC_API_KEY_HERE"
    # Screen/mission/expansion calls. 5-family models think by default and
    # max_tokens caps thinking+text together, so src/claude/api.py turns
    # thinking off (or effort down) for these small structured-JSON calls.
    claude_model: str = "claude-sonnet-5"
    # Deep-verify pass over ranking finalists only (~15-30 calls a run).
    claude_verify_model: str = "claude-opus-5"
    # CLAUDE_PROMPT_CACHE=0 disables prompt caching; CLAUDE_CACHE_TTL=1h
    # buys the 1-hour cache; CLAUDE_USAGE_SUMMARY=0 silences the exit line.
    claude_prompt_cache: bool = True
    claude_cache_ttl: Annotated[Literal["5m", "1h"],
                                BeforeValidator(str.lower)] = "5m"
    claude_usage_summary: bool = True
    # CareerOneStop (DOL) Web API, the National Labor Exchange feed. Register
    # at https://www.careeronestop.org/Developers/WebAPI/registration.aspx.
    careeronestop_user_id: str = ""
    careeronestop_token: str = ""
    # USAJOBS Search API; register at https://developer.usajobs.gov/apirequest/
    # USAJOBS_EMAIL must be the address the key was registered to. Search
    # scope lives in profile [sources.usajobs].
    usajobs_api_key: str = ""
    usajobs_email: str = ""
    # Where things live (src/config/paths.py, src/config/profile.py).
    jobs_data_dir: Path | None = None
    jobs_profile: Path | None = None
    jobs_resume: Path | None = None
    localappdata: Path | None = None
    xdg_data_home: Path | None = None
    # Web UI port (src/web/server.py; --port=N overrides).
    webui_port: int = Field(5533, ge=1, le=65535)
    # Thread-pool sizes; unset -> n_cpus - 1 (src/net/util.worker_count).
    crawler_workers: PositiveInt | None = None
    discovery_workers: PositiveInt | None = None
    harvest_workers: PositiveInt | None = None
    # Headless browsers for discovery's parallel JS fallback.
    js_browsers: int = 4

    @model_validator(mode="before")
    @classmethod
    def _blank_is_unset(cls, data):
        return {k: v.strip() if isinstance(v, str) else v
                for k, v in data.items()
                if not (isinstance(v, str) and not v.strip())}


def read_env():
    """A fresh Settings from the environment, or ValueError naming every bad
    variable (never its value: some are secrets)."""
    try:
        return Settings()
    except ValidationError as e:
        bad = "".join(f"\n  {x['loc'][0].upper()}: {x['msg']}" for x in
                      e.errors(include_url=False, include_input=False))
        raise ValueError(f"bad environment variable(s):{bad}") from None


def require_creds(source, register_url, **values):
    """The named credentials in the order given, or None with one line out.

    Every keyed source here needs SEVERAL env-backed values at once (a user
    id AND a token; an API key AND the address it was registered to) and is
    OPT-IN: no key means the source sits out, like a board that is down, and
    the rest of the crawl runs. Each fetcher had written that rule out with
    its own wording, its own indent and its own idea of what to say -- and
    with Settings' blank-is-unset rule re-implemented by hand, so a
    variable exported as "   " read as configured in one of them.

    `values` maps ENV VAR NAME -> the value the caller read from config (not
    read here: the fetchers take theirs off the config module so a test can
    monkeypatch it).

    >>> require_creds("ExampleJobs", "https://example.org/apikey",
    ...               EXAMPLE_KEY="  k  ", EXAMPLE_EMAIL="me@example.org")
    ('k', 'me@example.org')

    One blank value is enough to skip the source, and the line names every
    variable it wanted plus where to register:

    >>> require_creds("ExampleJobs", "https://example.org/apikey",
    ...               EXAMPLE_KEY="", EXAMPLE_EMAIL="me@example.org") is None
      [!] ExampleJobs skipped: set EXAMPLE_KEY and EXAMPLE_EMAIL
          (free, register at https://example.org/apikey).
    True
    """
    got = tuple((v or "").strip() for v in values.values())
    if all(got):
        return got
    names = " and ".join(values)
    print(f"  [!] {source} skipped: set {names}\n"
          f"      (free, register at {register_url}).")
    return None


SETTINGS = read_env()

GMAIL_ADDRESS = SETTINGS.gmail_address
GMAIL_APP_PASSWORD = SETTINGS.gmail_app_password
ANTHROPIC_API_KEY = SETTINGS.anthropic_api_key
CLAUDE_MODEL = SETTINGS.claude_model
CLAUDE_VERIFY_MODEL = SETTINGS.claude_verify_model
CAREERONESTOP_USER_ID = SETTINGS.careeronestop_user_id
CAREERONESTOP_TOKEN = SETTINGS.careeronestop_token
USAJOBS_API_KEY = SETTINGS.usajobs_api_key
USAJOBS_EMAIL = SETTINGS.usajobs_email
