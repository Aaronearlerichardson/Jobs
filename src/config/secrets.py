"""Secrets and model names: environment variables, with placeholders for
local development. Nothing here reads the profile or the filesystem.

    PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."
    cmd.exe:     set ANTHROPIC_API_KEY=sk-ant-...
    bash/zsh:    export ANTHROPIC_API_KEY=sk-ant-...
"""

import os


def env(name, default=""):
    """An env var's value, treating BLANK as unset.

    `os.environ.get(name, default)` returns "" when the variable exists but
    is empty — so `ANTHROPIC_API_KEY=""` (a CI runner exporting it, a shell
    profile clearing it) read as "a key is configured" and the scorers tried
    to authenticate with nothing instead of degrading to their offline
    fallbacks. Blank means absent everywhere in this package.

    >>> import os
    >>> os.environ["_CFG_DOCTEST"] = "   "
    >>> env("_CFG_DOCTEST", "fallback")
    'fallback'
    >>> os.environ["_CFG_DOCTEST"] = "  value  "
    >>> env("_CFG_DOCTEST", "fallback")
    'value'
    >>> del os.environ["_CFG_DOCTEST"]
    """
    return (os.environ.get(name) or "").strip() or default


def require_creds(source, register_url, **values):
    """The named credentials in the order given, or None with one line out.

    Every keyed source here needs SEVERAL env-backed values at once (a user
    id AND a token; an API key AND the address it was registered to) and is
    OPT-IN: no key means the source sits out, like a board that is down, and
    the rest of the crawl runs. Each fetcher had written that rule out with
    its own wording, its own indent and its own idea of what to say -- and
    with `env`'s blank-is-unset rule re-implemented by hand, so a variable
    exported as "   " read as configured in one of them.

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


# Digest email is opt-in and OFF until you set both of these — there is no
# built-in address. Blank GMAIL_ADDRESS simply disables emailing (src/digest/render.py).
GMAIL_ADDRESS      = env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD", "YOUR_APP_PASSWORD_HERE")
ANTHROPIC_API_KEY  = env("ANTHROPIC_API_KEY",  "YOUR_ANTHROPIC_API_KEY_HERE")
# Screen/mission/expansion calls: Sonnet 5 — near-Opus quality at Sonnet
# pricing ($3/$15 per MTok; intro $2/$10 through 2026-08-31, cheaper than the
# Sonnet 4.6 it replaces). NOTE for 5-family models: thinking is ON by
# default and max_tokens caps thinking+text together — src/claude/api.py
# disables thinking for these small structured-JSON calls.
CLAUDE_MODEL       = env("CLAUDE_MODEL", "claude-sonnet-5")
# Deep-verify pass over ranking finalists only (~15-30 calls/run, judgment-
# heavy): Opus 5 with adaptive thinking. $5/$25 per MTok, but bounded volume.
CLAUDE_VERIFY_MODEL = env("CLAUDE_VERIFY_MODEL", "claude-opus-5")

# CareerOneStop (DOL) Web API — free key exposes the National Labor Exchange
# (NLx) feed, where federal contractors must list openings (VEVRAA). Register
# at https://www.careeronestop.org/Developers/WebAPI/registration.aspx; DOL
# emails a UserId + token. Used by `python run_scraper.py --nlx "Meta,Google"`.
CAREERONESTOP_USER_ID = env("CAREERONESTOP_USER_ID")
CAREERONESTOP_TOKEN   = env("CAREERONESTOP_TOKEN")

# USAJOBS Search API — free key covers every federal opening, which no other
# source here can see (an agency lab runs no ATS and files nothing with the
# state job bank). Register at https://developer.usajobs.gov/apirequest/;
# OPM emails a key tied to the address you registered. USAJOBS_EMAIL must be
# that same address — the API takes it as the User-Agent and rejects a key
# sent with anything else. Search scope lives in profile [sources.usajobs].
USAJOBS_API_KEY = env("USAJOBS_API_KEY")
USAJOBS_EMAIL   = env("USAJOBS_EMAIL")
