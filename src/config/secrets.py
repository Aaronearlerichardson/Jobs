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
