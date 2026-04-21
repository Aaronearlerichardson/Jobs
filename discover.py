#!/usr/bin/env python3
"""
discover.py — expand the crawler's company universe.

Given a sector/industry/term, asks Claude for likely employers in that space,
then probes each public ATS (Greenhouse / Lever / Ashby / Kula) to confirm
which slugs are real. Outputs copy-paste dict entries you can drop straight
into crawler.py.

Also provides:
  * A credentials manager for gated sites (LinkedIn, Indeed, etc.).
  * A Playwright-based session capture tool that opens a real browser, lets
    you log in manually (handling 2FA etc. yourself), and saves the resulting
    cookies/localStorage to disk so later automated fetches can reuse the
    authenticated session WITHOUT ever storing your password.

See the ToS warnings near the bottom of this file before using gated fetchers.

Usage:
    python discover.py "neurotech startups"
    python discover.py "medical device companies hiring ML engineers"
    python discover.py --from-keywords          # seed from crawler.INCLUDE_KEYWORDS
    python discover.py --credentials-init       # scaffold credentials.json
    python discover.py --credentials-check      # print what's configured
    python discover.py --capture-session linkedin   # open browser, save session
    python discover.py --list-sessions          # show captured sessions + age
    python discover.py --test-session linkedin  # verify a saved session still works
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests

# Reuse config + helpers from crawler.py so there's one source of truth
from crawler import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    HEADERS,
    REPORT_DIR,
    SCRIPT_DIR,
    INCLUDE_KEYWORDS,
    _call_claude_json,
)

CREDENTIALS_PATH = SCRIPT_DIR / "credentials.json"
CREDENTIALS_TEMPLATE_PATH = SCRIPT_DIR / "credentials.json.template"
SESSION_DIR = SCRIPT_DIR / "sessions"   # storage_state JSON files live here

# ─── Claude prompt ────────────────────────────────────────────────────────────

_DISCOVER_SYSTEM = """You are a technical recruiter who maps employers to ATS platforms. Given a sector, industry, or job concept, list companies that (a) plausibly hire for roles in that space and (b) are likely to post jobs publicly. The user is targeting neurotechnology / BCI / ML / signal-processing roles and does NOT have a PhD.

Return ONLY a JSON object with this exact shape:
{
  "companies": [
    {
      "name": "Full company name",
      "ats": "greenhouse" | "lever" | "ashby" | "kula" | "workday" | "unknown",
      "slug_guess": "likely-slug-on-that-ats-or-null",
      "careers_url": "https://…",
      "notes": "One short sentence on why this company fits."
    }
  ],
  "gated_sites": [
    {
      "site": "linkedin" | "indeed" | "builtin" | "wellfound",
      "query": "search query a user could run there",
      "notes": "What makes this site worth the auth hassle for this sector."
    }
  ]
}

Rules:
- Up to 15 companies. Prefer ones where a Research Engineer / ML Engineer / Software Engineer role is achievable without a PhD.
- slug_guess: best educated guess (typically the company name lowercased with hyphens). Use null if you really can't guess.
- ats: "unknown" is fine if you're not sure.
- Return ONLY valid JSON. No markdown, no commentary."""


# ─── ATS probes ───────────────────────────────────────────────────────────────

def probe_greenhouse(slug: str) -> tuple[bool, int]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        return (True, len(r.json().get("jobs", [])))
    except Exception:
        return (False, 0)


def probe_lever(slug: str) -> tuple[bool, int]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        data = r.json()
        return (True, len(data) if isinstance(data, list) else 0)
    except Exception:
        return (False, 0)


def probe_ashby(slug: str) -> tuple[bool, int]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        return (True, len(r.json().get("jobPostings", [])))
    except Exception:
        return (False, 0)


def probe_kula(slug: str) -> tuple[bool, int]:
    url = f"https://careers.kula.ai/{slug}"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        return (r.status_code == 200 and len(r.text) > 1000, 0)
    except Exception:
        return (False, 0)


PROBES = {
    "greenhouse": probe_greenhouse,
    "lever":      probe_lever,
    "ashby":      probe_ashby,
    "kula":       probe_kula,
}


# ─── Discovery pipeline ───────────────────────────────────────────────────────

@dataclass
class Candidate:
    name: str
    ats: str
    slug_guess: str | None
    careers_url: str
    notes: str
    confirmed: bool = False
    job_count: int = 0
    tried_slugs: list[str] = field(default_factory=list)


def candidate_from_dict(d: dict) -> Candidate:
    return Candidate(
        name        = d.get("name", "").strip(),
        ats         = (d.get("ats") or "unknown").lower(),
        slug_guess  = (d.get("slug_guess") or None),
        careers_url = d.get("careers_url", "").strip(),
        notes       = d.get("notes", "").strip(),
    )


def slug_variants(name: str, first_guess: str | None) -> list[str]:
    base = (name or "").lower().strip()
    variants = []
    if first_guess:
        variants.append(first_guess.lower())
    variants += [
        base.replace(" ", "-"),
        base.replace(" ", ""),
        base.replace(",", "").replace(".", "").replace(" ", "-"),
        base.split()[0] if base else "",
    ]
    seen, out = set(), []
    for v in variants:
        v = v.strip("-")
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out[:4]


def validate_candidate(c: Candidate, delay: float = 0.3) -> Candidate:
    probe = PROBES.get(c.ats)
    if not probe:
        return c
    for slug in slug_variants(c.name, c.slug_guess):
        c.tried_slugs.append(slug)
        ok, count = probe(slug)
        time.sleep(delay)
        if ok:
            c.confirmed = True
            c.slug_guess = slug
            c.job_count = count
            return c
    return c


def discover(term: str) -> dict:
    print(f"  ▸ Asking Claude for companies in: {term!r}")
    payload = _call_claude_json(_DISCOVER_SYSTEM, term, max_tokens=2000)
    if not payload:
        return {"term": term, "companies": [], "gated_sites": []}

    raw_companies = payload.get("companies", [])
    print(f"  ▸ Claude returned {len(raw_companies)} company suggestion(s)")
    validated = []
    for i, rc in enumerate(raw_companies, 1):
        cand = candidate_from_dict(rc)
        print(f"  [{i}/{len(raw_companies)}] {cand.name} ({cand.ats})")
        validated.append(validate_candidate(cand))

    return {
        "term":        term,
        "companies":   validated,
        "gated_sites": payload.get("gated_sites", []),
    }


# ─── Report writing ───────────────────────────────────────────────────────────

def write_discovery_report(result: dict) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    slug = "".join(c if c.isalnum() else "_" for c in result["term"].lower())[:40]
    path = REPORT_DIR / f"discover_{date_str}_{slug}.md"

    companies = result["companies"]
    confirmed = [c for c in companies if c.confirmed]
    unconfirmed = [c for c in companies if not c.confirmed]

    by_ats: dict[str, list[Candidate]] = {}
    for c in confirmed:
        by_ats.setdefault(c.ats, []).append(c)

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Company Discovery — {result['term']}\n\n")
        f.write(f"_Generated {date_str}_\n\n")
        f.write(f"**{len(confirmed)}** confirmed / {len(companies)} suggested\n\n")

        if confirmed:
            f.write("## Confirmed — ready to add to crawler.py\n\n")
            for ats_name, cands in by_ats.items():
                dict_name = {
                    "greenhouse": "GREENHOUSE_COMPANIES",
                    "lever":      "LEVER_COMPANIES",
                    "ashby":      "ASHBY_COMPANIES",
                    "kula":       "KULA_COMPANIES",
                }.get(ats_name, f"{ats_name.upper()}_COMPANIES")
                f.write(f"### `{dict_name}`\n\n```python\n")
                for c in cands:
                    if ats_name == "kula":
                        f.write(f'    ("{c.name}", "{c.slug_guess}"),\n')
                    else:
                        f.write(f'    "{c.slug_guess}": "{c.name}",  '
                                f'# {c.job_count} job(s) live\n')
                f.write("```\n\n")

        if unconfirmed:
            f.write("## Unconfirmed — manual investigation needed\n\n")
            f.write("| Company | ATS guess | Slugs tried | Careers URL | Notes |\n")
            f.write("|---|---|---|---|---|\n")
            for c in unconfirmed:
                tried = ", ".join(f"`{s}`" for s in c.tried_slugs) or "—"
                f.write(f"| {c.name} | {c.ats} | {tried} | "
                        f"{c.careers_url or '—'} | {c.notes} |\n")
            f.write("\n")

        gated = result.get("gated_sites", [])
        if gated:
            f.write("## Gated sites (require auth)\n\n")
            f.write("Login-only boards Claude thinks are worth searching. "
                    "Use `python discover.py --capture-session <site>` to "
                    "save an authenticated session for these.\n\n")
            f.write("| Site | Suggested query | Notes |\n|---|---|---|\n")
            for g in gated:
                f.write(f"| {g.get('site','?')} | `{g.get('query','')}` | {g.get('notes','')} |\n")

    print(f"\n  Report → {path}\n")
    return path


def print_summary(result: dict) -> None:
    companies = result["companies"]
    confirmed = [c for c in companies if c.confirmed]
    w = 62
    print(f"\n{'═'*w}")
    print(f"  Discovery: '{result['term']}'")
    print(f"{'═'*w}")
    print(f"  Confirmed: {len(confirmed)} / Suggested: {len(companies)}\n")
    for c in confirmed:
        print(f"    ✓ {c.name:<30} {c.ats:<10} slug='{c.slug_guess}'  ({c.job_count} jobs)")
    unconfirmed = [c for c in companies if not c.confirmed]
    if unconfirmed:
        print(f"\n  Unconfirmed ({len(unconfirmed)}):")
        for c in unconfirmed:
            print(f"    ? {c.name:<30} {c.ats:<10} tried={c.tried_slugs}")
    print(f"{'═'*w}\n")


# ─── Credentials manager (legacy cookie-based) ────────────────────────────────
#
# The credentials.json approach is preserved for users who want to paste raw
# cookies (e.g. li_at from DevTools). It's being superseded by session capture
# (see next section) which handles login in a real browser and stores the
# full storage_state — no password ever touches your disk.

CREDENTIAL_SCHEMA = {
    "_description": (
        "Credentials for gated job sites. Each block is optional. "
        "Delete blocks you don't need. NEVER commit this file. "
        "Most users should prefer --capture-session instead."
    ),
    "_tos_warning": (
        "Automated access to LinkedIn/Indeed/Glassdoor/Wellfound violates "
        "their ToS. Your account may be suspended. Use at your own risk."
    ),
    "linkedin": {
        "_how": "Log into linkedin.com in a browser, open DevTools → "
                "Application → Cookies → copy the value of 'li_at'.",
        "li_at": "",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    },
    "indeed": {
        "_how": "Indeed uses a CF_CLEARANCE cookie plus a session cookie. "
                "Capture both after logging in.",
        "cf_clearance": "",
        "session_cookie": "",
    },
    "wellfound": {
        "_how": "Wellfound (AngelList Talent) uses _wellfound cookie.",
        "_wellfound": "",
    },
    "custom": {
        "_how": "Catch-all for any other site. Fetcher reads these keys by name."
    },
}


def init_credentials_template() -> Path:
    if CREDENTIALS_TEMPLATE_PATH.exists():
        print(f"  Template already exists: {CREDENTIALS_TEMPLATE_PATH}")
    else:
        CREDENTIALS_TEMPLATE_PATH.write_text(
            json.dumps(CREDENTIAL_SCHEMA, indent=2), encoding="utf-8"
        )
        print(f"  Wrote template → {CREDENTIALS_TEMPLATE_PATH}")

    if CREDENTIALS_PATH.exists():
        print(f"  credentials.json already exists — not overwriting.")
    else:
        CREDENTIALS_PATH.write_text(
            json.dumps(CREDENTIAL_SCHEMA, indent=2), encoding="utf-8"
        )
        print(f"  Scaffold → {CREDENTIALS_PATH}")
        print(f"  Edit this file, then confirm it's in .gitignore before committing anything.")
    return CREDENTIALS_PATH


def load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [!] credentials.json is malformed: {e}")
        return {}


def get_credentials(site: str) -> dict:
    return load_credentials().get(site, {}) or {}


def check_credentials() -> None:
    creds = load_credentials()
    if not creds:
        print("  No credentials.json found. Run: python discover.py --credentials-init")
        return
    print(f"\n  credentials.json — {CREDENTIALS_PATH}")
    for site, block in creds.items():
        if site.startswith("_"):
            continue
        if not isinstance(block, dict):
            continue
        filled = [k for k, v in block.items()
                  if not k.startswith("_") and isinstance(v, str) and v]
        status = f"{len(filled)} value(s) set" if filled else "empty"
        print(f"    • {site:<12} {status}")
    print()


# ─── Playwright session capture ───────────────────────────────────────────────
#
# Goal: never touch the user's password. Launch a real visible Chromium,
# navigate to the site's login page, wait for the user to finish logging in
# (including 2FA, CAPTCHA, device challenges, whatever), then persist the
# browser context's storage_state to disk. Subsequent fetches load that JSON
# and skip the login entirely.
#
# This is strictly better than the cookie-paste flow because:
#   * Password never stored anywhere (plaintext or otherwise).
#   * 2FA / captcha / email challenges are handled by the human, not bypassed.
#   * Cookie rotation is automatic (the browser's own refresh mechanisms run).
#   * storage_state captures BOTH cookies AND localStorage — some sites (e.g.
#     Wellfound) keep auth state in localStorage, not just cookies.
#
# Best-practice measures baked in:
#   * playwright-stealth applied to every page — patches the obvious
#     fingerprint giveaways (navigator.webdriver, languages, plugins,
#     window.chrome, WebGL vendor, chrome.runtime, permissions query,
#     etc.) without us maintaining our own patchset.
#   * Real Chrome User-Agent matching a recent stable release.
#   * Realistic viewport + locale + timezone.
#   * Jittered delays between actions (no robotic cadence).
#   * After capture, a verification fetch confirms the session works.
#   * Test mode re-checks session freshness without re-login.
#
# ToS: scraping authenticated LinkedIn/Indeed/etc. violates their ToS. Your
# account can be restricted or banned. This tool reduces the risk of password
# theft but does NOT reduce the ToS risk. Use at your own risk.

SITE_CONFIGS = {
    # Verification is URL-based: after going to `verify_url`, if the final
    # URL (after any redirects) CONTAINS any substring in `logged_out_url_markers`
    # we're logged out; if it contains any in `logged_in_url_markers` or matches
    # `verify_url` itself, we're logged in. URL checks are far more stable than
    # HTML class names, which these sites change monthly.
    "linkedin": {
        "login_url":  "https://www.linkedin.com/login",
        "verify_url": "https://www.linkedin.com/feed/",
        "logged_in_url_markers":  ["/feed"],
        "logged_out_url_markers": ["/login", "/uas/login", "/checkpoint", "/authwall"],
    },
    "indeed": {
        "login_url":  "https://secure.indeed.com/auth",
        "verify_url": "https://myjobs.indeed.com/",
        "logged_in_url_markers":  ["myjobs.indeed.com"],
        "logged_out_url_markers": ["/auth", "/account/login"],
    },
    "wellfound": {
        "login_url":  "https://wellfound.com/login",
        "verify_url": "https://wellfound.com/jobs",
        "logged_in_url_markers":  ["/jobs", "/candidate", "/user"],
        "logged_out_url_markers": ["/login", "/signup"],
    },
}

# Recent Chrome on Windows — keep roughly current. A stale UA is a red flag.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

def _require_browser():
    """
    Lazily import both playwright and playwright-stealth. Returns a factory
    that opens a stealthed Playwright context manager.

    Usage:
        open_pw = _require_browser()
        with open_pw() as pw:
            browser = pw.chromium.launch(...)
            ...

    Handles both playwright-stealth APIs:
      * v2.x — Stealth().use_sync(sync_playwright())    [preferred]
      * v1.x — stealth_sync(page) applied after new_page()
    In v1 mode the returned factory is still a plain sync_playwright() context
    manager, and the caller is expected to call _stealth_page(page) too.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "\n  [!] playwright is not installed.\n"
            "  Install with:\n"
            "      pip install playwright playwright-stealth\n"
            "      playwright install chromium\n"
        )
        sys.exit(1)

    # Try v2 API first
    try:
        from playwright_stealth import Stealth
        return lambda: Stealth().use_sync(sync_playwright())
    except ImportError:
        pass

    # Fall back to v1 API (we'll stealth each page manually)
    try:
        from playwright_stealth import stealth_sync  # noqa: F401
        # Stash the function on the module so _stealth_page() can find it
        import playwright_stealth  # noqa: F401
        return sync_playwright
    except ImportError:
        print(
            "\n  [!] playwright-stealth is not installed.\n"
            "  Install with:\n"
            "      pip install playwright-stealth\n"
        )
        sys.exit(1)


def _stealth_page(page) -> None:
    """
    No-op when using v2 API (pages come pre-stealthed from use_sync).
    Applies stealth_sync when falling back to v1 API.
    """
    try:
        from playwright_stealth import Stealth  # noqa: F401
        return   # v2 path — use_sync handled it
    except ImportError:
        pass
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except ImportError:
        pass   # already errored in _require_browser


# Track the user's browser choice. Set by main() from the --browser flag.
# "chrome"   = real installed Google Chrome via channel="chrome" (best for
#              Cloudflare — Playwright's bundled Chromium has a "HeadlessChrome"
#              build signature that Cloudflare flags).
# "chromium" = Playwright's bundled Chromium (most likely to trip Turnstile).
# "firefox"  = Playwright's bundled Firefox (different fingerprint, sometimes
#              works where Chromium doesn't, but no guarantee).
_BROWSER_CHOICE = "chrome"


_CHROMIUM_DEANON_ARGS = {
    # Strip Playwright's default flags that either show a Chrome banner or
    # advertise automation to fingerprinters / Cloudflare:
    #   --enable-automation : "controlled by automated test software" banner +
    #                         navigator.webdriver = true
    #   --no-sandbox        : "you are using an unsupported command-line flag"
    #                         banner; not needed on Windows desktop
    #   --disable-extensions: kills all profile extensions, which is bad for
    #                         persistent-profile mode (fewer trust signals)
    "ignore_default_args": [
        "--enable-automation",
        "--no-sandbox",
        "--disable-extensions",
    ],
    "args": [
        "--disable-blink-features=AutomationControlled",
        "--no-default-browser-check",
    ],
}


def _launch_browser(pw, *, headless: bool):
    """
    Launch the configured browser engine. Returns a Browser instance.
    Falls back gracefully: if 'chrome' channel isn't installed, prints a
    helpful message and tries Playwright's bundled Chromium instead.
    """
    choice = _BROWSER_CHOICE
    if choice == "firefox":
        # Firefox doesn't use the Chromium automation flag set.
        return pw.firefox.launch(headless=headless)
    if choice == "chrome":
        try:
            return pw.chromium.launch(
                headless=headless, channel="chrome", **_CHROMIUM_DEANON_ARGS,
            )
        except Exception as e:
            print(f"  [!] Couldn't launch real Chrome ({e}).")
            print(f"      Install with:  playwright install chrome")
            print(f"      Falling back to bundled Chromium (Cloudflare may block).")
            return pw.chromium.launch(headless=headless, **_CHROMIUM_DEANON_ARGS)
    # default / "chromium"
    return pw.chromium.launch(headless=headless, **_CHROMIUM_DEANON_ARGS)


# ─── Persistent profile (real Chrome user data) ───────────────────────────────
# When Cloudflare Turnstile keeps failing even with --browser chrome, the next
# escalation is to use the user's REAL Chrome profile — cookies, history,
# extensions, the works. From Cloudflare's perspective this looks like a
# longtime account with browsing history, not an ephemeral automation instance.
#
# IMPORTANT: Chrome must be CLOSED before launching with the profile. Playwright
# can't open a profile that's locked by another Chrome process.

_USE_PROFILE = False
_USER_DATA_DIR: str | None = None
_PROFILE_DIRECTORY = "Default"   # the subfolder name; "Default", "Profile 1", etc.
_REFRESH_PROFILE = False         # set to True by --refresh-profile

# Where we keep the profile copy. Chrome won't allow CDP on its DEFAULT
# user-data dir, so we maintain a separate copy that CDP can drive.
PROFILE_COPY_DIR = SCRIPT_DIR / "sessions" / "chrome-profile"


def _default_chrome_user_data_dir() -> str | None:
    """Best-effort autodetect of the Chrome user-data directory per OS."""
    import os, platform
    sysname = platform.system()
    if sysname == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return str(Path(local) / "Google" / "Chrome" / "User Data")
    elif sysname == "Darwin":
        return str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome")
    else:
        return str(Path.home() / ".config" / "google-chrome")
    return None


def _prepare_profile_copy(*, refresh: bool = False) -> str:
    """
    Copy the user's real Chrome profile to a non-default location so CDP
    (the remote-debugging pipe Playwright uses to control the browser) works.

    Returns the path to the copy.

    Chrome refuses to start its remote-debugging server on the default
    user-data directory — the "DevTools remote debugging requires a
    non-default data directory" error. This helper sidesteps that by
    maintaining a parallel profile under SCRIPT_DIR/sessions/chrome-profile/.

    Skips cache / GPUCache / Service Worker caches (tens of MB of disposable
    data) to keep the copy fast. Cookies, history, Local Storage, and Login
    Data are all preserved — these are what Cloudflare / LinkedIn care about.
    """
    import shutil

    src = _USER_DATA_DIR or _default_chrome_user_data_dir()
    if not src:
        print("  [!] Can't autodetect Chrome profile. Pass --user-data-dir explicitly.")
        sys.exit(1)

    src_path = Path(src)
    if not src_path.exists():
        print(f"  [!] Chrome profile not found at: {src}")
        print(f"      Pass --user-data-dir to point at the right location.")
        sys.exit(1)

    PROFILE_COPY_DIR.parent.mkdir(parents=True, exist_ok=True)

    if PROFILE_COPY_DIR.exists() and not refresh:
        print(f"  Reusing profile copy at {PROFILE_COPY_DIR}")
        print("  (Pass --refresh-profile to re-copy from your live profile.)")
        return str(PROFILE_COPY_DIR)

    if PROFILE_COPY_DIR.exists():
        print("  Removing old profile copy...")
        shutil.rmtree(PROFILE_COPY_DIR, ignore_errors=True)

    print(f"\n  Copying your Chrome profile → {PROFILE_COPY_DIR}")
    print(f"  (Preserves cookies/history/login data. May take 10-60s.)")
    print(f"  Chrome must be CLOSED or some files will be locked.\n")

    # Skip cache directories — they're large, disposable, and not needed for
    # session-state purposes.
    _SKIP_DIR_NAMES = {
        "cache", "code cache", "gpucache", "dawncache", "shadercache",
        "service worker", "subresource filter", "optimization guide",
        "file system", "indexeddb", "webstorage",
        "crashpad", "guestprofile", "system profile",
    }

    def _ignore(dirpath, entries):
        out = set()
        for e in entries:
            if e.lower() in _SKIP_DIR_NAMES:
                out.add(e)
            # Also skip lock files that Chrome leaves behind
            if e in ("SingletonCookie", "SingletonLock", "SingletonSocket",
                     "lockfile", "LOCK"):
                out.add(e)
        return out

    try:
        shutil.copytree(src_path, PROFILE_COPY_DIR, ignore=_ignore,
                        dirs_exist_ok=True)
    except PermissionError as e:
        print(f"  [!] Copy failed — file locked: {e}")
        print(f"      Chrome is still running. Quit it fully and retry.")
        sys.exit(1)
    except Exception as e:
        # Partial copies are acceptable — some individual files may be locked
        # even when the browser is closed (e.g. Secure Preferences on Windows).
        print(f"  [!] Partial copy (continuing anyway): {e}")

    return str(PROFILE_COPY_DIR)


def _clear_chrome_locks(user_dir: str) -> None:
    """
    Remove the Singleton lock files Chrome leaves in a user-data dir after
    every run. If these are present at launch time, Chrome assumes another
    Chrome instance is running and forwards its launch request to that
    instance, then exits. From Playwright's perspective Chrome dies
    immediately after launch and you get "Browser window not found".

    These files are SAFE to delete — they're just cooperative locks.
    """
    lock_names = [
        "SingletonCookie", "SingletonLock", "SingletonSocket",
        "lockfile", "LOCK",
    ]
    base = Path(user_dir)
    removed = []
    for name in lock_names:
        for p in base.glob(name):
            try:
                p.unlink()
                removed.append(p.name)
            except Exception:
                pass
        # Some are symlinks on macOS/Linux
        try:
            for p in base.glob(f"{name}*"):
                if p.is_symlink():
                    p.unlink()
                    removed.append(p.name)
        except Exception:
            pass
    if removed:
        print(f"  Cleared stale Chrome lock files: {', '.join(sorted(set(removed)))}")


def _reset_chrome_session_state(user_dir: str, profile_dir: str = "Default") -> None:
    """
    Reset the per-profile state Chrome writes when it exits, so the next
    launch starts cleanly. Without this, the second launch of a reused
    profile copy fails with "Browser window not found" — Chrome enters a
    recovery flow that doesn't play with Playwright's CDP control.

    Two parts:
      1. Patch Preferences so exited_cleanly=true and exit_type="Normal".
         Otherwise Chrome thinks it crashed and either shows a recovery
         dialog or opens 0 windows.
      2. Delete Last/Current Session/Tabs files so Chrome doesn't try to
         restore tabs from the previous run.
    """
    profile_path = Path(user_dir) / profile_dir
    if not profile_path.exists():
        return

    # 1. Patch Preferences
    pref_file = profile_path / "Preferences"
    if pref_file.exists():
        try:
            prefs = json.loads(pref_file.read_text(encoding="utf-8"))
            prof = prefs.setdefault("profile", {})
            changed = False
            if prof.get("exited_cleanly") is not True:
                prof["exited_cleanly"] = True
                changed = True
            if prof.get("exit_type") != "Normal":
                prof["exit_type"] = "Normal"
                changed = True
            if changed:
                pref_file.write_text(json.dumps(prefs), encoding="utf-8")
        except Exception as e:
            print(f"  [warn] Couldn't patch Preferences ({e}). Continuing.")

    # 2. Delete session-restore state
    stale_files = ["Last Session", "Last Tabs", "Current Session", "Current Tabs"]
    for name in stale_files:
        p = profile_path / name
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    # And the Sessions folder (newer Chrome stores session state here too)
    sessions_dir = profile_path / "Sessions"
    if sessions_dir.exists():
        try:
            import shutil
            shutil.rmtree(sessions_dir, ignore_errors=True)
        except Exception:
            pass


def _open_context(pw, *, headless: bool, storage_state: str | None = None):
    """
    Returns (context, closer) — closer() is what you call when done.

    Two modes:
      * ephemeral (default): launch a Browser, then new_context(). Closer
        closes the browser.
      * persistent (--use-profile): launch_persistent_context() with the user's
        real Chrome user-data dir. storage_state is ignored — the profile has
        its own cookies. Closer closes the context directly.
    """
    if _USE_PROFILE:
        # Always launch against a COPY of the profile — Chrome blocks CDP
        # on its default user-data dir.
        user_dir = _prepare_profile_copy(refresh=_REFRESH_PROFILE)
        _clear_chrome_locks(user_dir)
        _reset_chrome_session_state(user_dir, _PROFILE_DIRECTORY)

        # launch_persistent_context returns a BrowserContext directly (no Browser).
        #
        # We strip Playwright's default automation flags so Chrome doesn't show
        # the "controlled by automated test software" banner and so
        # navigator.webdriver isn't true. These same flags are what Cloudflare
        # checks. playwright-stealth patches the page runtime, but it can't
        # un-set launch-time flags.
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=user_dir,
                headless=headless,
                channel="chrome",
                ignore_default_args=_CHROMIUM_DEANON_ARGS["ignore_default_args"],
                args=[
                    f"--profile-directory={_PROFILE_DIRECTORY}",
                    *_CHROMIUM_DEANON_ARGS["args"],
                ],
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
                # Don't override user_agent here — the profile has its own.
            )
        except Exception as e:
            msg = str(e)
            print(f"  [!] launch_persistent_context failed: {msg}")
            if "ProcessSingleton" in msg or "already in use" in msg:
                print("\n  >>> Chrome is currently running. Quit it completely")
                print("      (check the system tray) and re-run this command.\n")
            elif "Executable doesn't exist" in msg or "channel" in msg.lower():
                print("\n  >>> Real Chrome not installed for Playwright. Run:")
                print("      playwright install chrome\n")
            sys.exit(1)
        return context, context.close

    # Ephemeral path (existing behavior)
    browser = _launch_browser(pw, headless=headless)
    context = browser.new_context(
        user_agent=_DEFAULT_UA,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        storage_state=storage_state,
    )
    return context, browser.close


def _session_path(site: str) -> Path:
    SESSION_DIR.mkdir(exist_ok=True)
    return SESSION_DIR / f"{site}.json"


def _jitter(min_ms: int = 500, max_ms: int = 1500) -> None:
    time.sleep(random.uniform(min_ms, max_ms) / 1000.0)


def _safe_goto(page, url: str, *, timeout_ms: int = 20000) -> str | None:
    """
    Navigate to URL, tolerating SPA / client-side redirects that interrupt
    the navigation. Returns the final URL (after settling), or None on real
    failure.

    LinkedIn / Indeed do client-side bounces (e.g. /feed → /feed?trk=…) that
    raise "Navigation to X is interrupted by another navigation to Y" inside
    page.goto(). We treat those as success and read the final URL.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as e:
        msg = str(e)
        if "interrupted by another navigation" not in msg \
           and "Navigation timeout" not in msg:
            print(f"  [!] {url} navigation failed: {e}")
            return None
        # SPA bounce — try to wait for things to settle
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    try:
        return page.url
    except Exception:
        return None


def _classify_url(final_url: str | None, cfg: dict) -> str:
    """Returns 'in', 'out', or 'unknown' based on URL substring matches."""
    if not final_url:
        return "unknown"
    low = final_url.lower()
    for m in cfg.get("logged_out_url_markers", []):
        if m.lower() in low:
            return "out"
    for m in cfg.get("logged_in_url_markers", []):
        if m.lower() in low:
            return "in"
    return "unknown"


def capture_session(site: str) -> Path | None:
    """Open a visible browser, let user log in, save storage_state to disk."""
    if site not in SITE_CONFIGS:
        print(f"  [!] Unknown site: {site!r}")
        print(f"      Known sites: {', '.join(SITE_CONFIGS)}")
        return None

    cfg = SITE_CONFIGS[site]
    open_pw = _require_browser()
    out_path = _session_path(site)

    with open_pw() as pw:
        # _open_context() may print "Copying profile…" first when --use-profile.
        # Don't pre-announce; print the "log in now" prompt AFTER context opens.
        context, close_browser = _open_context(pw, headless=False)
        page = context.new_page()
        _stealth_page(page)

        mode = "real Chrome profile" if _USE_PROFILE else _BROWSER_CHOICE
        print(f"\n  Opening {site} login page in {mode}…")
        print(f"  Login URL: {cfg['login_url']}")

        try:
            page.goto(cfg["login_url"], wait_until="domcontentloaded")
        except Exception as e:
            # Some sites SPA-bounce on the login page too; treat as soft.
            if "interrupted by another navigation" not in str(e):
                print(f"  [!] Couldn't open login URL: {e}")
                close_browser()
                return None

        if _USE_PROFILE:
            print("  You may already be logged in (using your real profile).")
            print("  If not, log in normally (2FA etc. all fine).")
        else:
            print("  Log in normally in the browser window (2FA etc. all fine).")
        print("  When you're fully logged in, return here and press Enter.\n")

        input("  >>> Press Enter AFTER you've completed login <<< ")

        _jitter()
        final_url = _safe_goto(page, cfg["verify_url"])
        _jitter()

        verdict = _classify_url(final_url, cfg)

        if verdict == "out":
            print(f"  [!] Verification: NOT logged in.")
            print(f"      After visiting {cfg['verify_url']}, you ended up at:")
            print(f"      {final_url}")
            ans = input("      Save session anyway? [y/N]: ").strip().lower()
            if ans != "y":
                close_browser()
                print("  Aborted. No session saved.")
                return None
        elif verdict == "in":
            print(f"  ✓ Login verified — landed at {final_url}")
        else:
            print(f"  ? Login unverified — landed at {final_url or '(unknown)'}.")
            print(f"    Saving anyway. Run --test-session {site} to confirm.")

        context.storage_state(path=str(out_path))
        close_browser()

    print(f"\n  Session saved → {out_path}")
    print("  File is gitignored by default. Do NOT commit it.")
    print(f"  Verify with:  python discover.py --test-session {site}")
    return out_path


def test_session(site: str) -> bool:
    """Re-open the browser with saved state and confirm it's still valid."""
    if site not in SITE_CONFIGS:
        print(f"  [!] Unknown site: {site!r}")
        return False

    path = _session_path(site)
    if not path.exists():
        print(f"  [!] No saved session for {site!r}. Run: "
              f"python discover.py --capture-session {site}")
        return False

    cfg = SITE_CONFIGS[site]
    open_pw = _require_browser()

    print(f"  Loading saved session for {site}…")
    with open_pw() as pw:
        context, close_browser = _open_context(
            pw, headless=True, storage_state=str(path),
        )
        page = context.new_page()
        _stealth_page(page)

        final_url = _safe_goto(page, cfg["verify_url"])
        _jitter()
        close_browser()

    verdict = _classify_url(final_url, cfg)
    age = _session_age_str(path)

    if verdict == "in":
        print(f"  ✓ Session valid — landed at {final_url} (captured {age})")
        return True
    if verdict == "out":
        print(f"  ✗ Session expired — bounced to {final_url}")
        print(f"    Re-run: python discover.py --capture-session {site} --use-profile")
        return False
    print(f"  ? Inconclusive — landed at {final_url or '(unknown)'}.")
    print(f"    Site may have changed URL conventions.")
    return False


def list_sessions() -> None:
    if not SESSION_DIR.exists():
        print("  No sessions captured yet.")
        print("  Run: python discover.py --capture-session linkedin")
        return
    files = sorted(SESSION_DIR.glob("*.json"))
    if not files:
        print("  No sessions captured yet.")
        return
    print(f"\n  Captured sessions — {SESSION_DIR}")
    for p in files:
        site = p.stem
        age = _session_age_str(p)
        kb = p.stat().st_size // 1024
        print(f"    • {site:<12} {age:<20} {kb} KB")
    print()


def _session_age_str(p: Path) -> str:
    mtime = datetime.fromtimestamp(p.stat().st_mtime)
    delta = datetime.now() - mtime
    if delta.days >= 1:
        return f"{delta.days}d ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours}h ago"
    mins = delta.seconds // 60
    return f"{mins}m ago"


def fetch_gated(site: str, url: str, *, timeout_ms: int = 20000) -> str | None:
    """
    Load a saved session and fetch a URL, returning HTML content.

    Intended for downstream gated fetchers (not yet wired into crawler.py).
    Returns None on failure.

    Rate-limiting note: callers should space requests generously. A sane
    default is 1 request per 5-15s with jitter, backing off on any 429/503.
    """
    path = _session_path(site)
    if not path.exists():
        print(f"  [!] No session for {site!r}. Run --capture-session first.")
        return None

    open_pw = _require_browser()
    with open_pw() as pw:
        context, close_browser = _open_context(
            pw, headless=True, storage_state=str(path),
        )
        page = context.new_page()
        _stealth_page(page)

        try:
            _jitter(1000, 3000)
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if resp and resp.status in (429, 503):
                print(f"  [!] {site} returned {resp.status}. Back off and retry later.")
                close_browser()
                return None
            _jitter(500, 1500)
            html = page.content()
        except Exception as e:
            print(f"  [!] fetch_gated({site}, {url}) failed: {e}")
            close_browser()
            return None

        close_browser()
        return html


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Expand the crawler's company universe.")
    ap.add_argument("term", nargs="?",
                    help="Sector/industry/term to search for (e.g. 'neurotech startups')")
    ap.add_argument("--from-keywords", action="store_true",
                    help="Run discovery for each entry in crawler.INCLUDE_KEYWORDS")
    ap.add_argument("--no-report", action="store_true",
                    help="Print to stdout only, don't write a markdown report")

    g = ap.add_argument_group("credentials (legacy cookie-paste flow)")
    g.add_argument("--credentials-init", action="store_true",
                   help="Create credentials.json.template and scaffold credentials.json")
    g.add_argument("--credentials-check", action="store_true",
                   help="Show which credential blocks are populated")

    s = ap.add_argument_group("session capture (preferred; Playwright)")
    s.add_argument("--capture-session", metavar="SITE",
                   help=f"Open a browser, log in manually, save session "
                        f"(sites: {', '.join(SITE_CONFIGS)})")
    s.add_argument("--test-session", metavar="SITE",
                   help="Verify a saved session is still valid")
    s.add_argument("--list-sessions", action="store_true",
                   help="Show captured sessions and their age")
    s.add_argument("--browser", choices=["chrome", "chromium", "firefox"],
                   default="chrome",
                   help="Which browser engine to use. Default 'chrome' uses your "
                        "real installed Chrome (best for Cloudflare). 'chromium' "
                        "uses Playwright's bundled test build. 'firefox' is an "
                        "alternative if Chrome fingerprinting is the problem.")
    s.add_argument("--use-profile", action="store_true",
                   help="Launch Chrome with a COPY of your real user profile "
                        "(cookies, history, extensions). Strongest Cloudflare "
                        "bypass. Chrome must be CLOSED first. The copy lives "
                        "at sessions/chrome-profile/ and is reused across runs. "
                        "Implies --browser chrome.")
    s.add_argument("--refresh-profile", action="store_true",
                   help="With --use-profile, re-copy from the live Chrome "
                        "profile instead of reusing the cached copy. Use this "
                        "after logging into a site in regular Chrome.")
    s.add_argument("--user-data-dir", metavar="PATH",
                   help="Override the SOURCE Chrome user-data dir to copy from. "
                        "Defaults to the OS standard location (Windows: "
                        "%%LOCALAPPDATA%%\\Google\\Chrome\\User Data). Only "
                        "used with --use-profile.")
    s.add_argument("--profile-directory", metavar="NAME", default="Default",
                   help="Profile subfolder name (e.g. 'Default', 'Profile 1'). "
                        "Only used with --use-profile.")

    args = ap.parse_args()

    global _BROWSER_CHOICE, _USE_PROFILE, _USER_DATA_DIR, _PROFILE_DIRECTORY, _REFRESH_PROFILE
    _BROWSER_CHOICE = args.browser
    _USE_PROFILE = args.use_profile
    _USER_DATA_DIR = args.user_data_dir
    _PROFILE_DIRECTORY = args.profile_directory
    _REFRESH_PROFILE = args.refresh_profile
    if _USE_PROFILE:
        _BROWSER_CHOICE = "chrome"   # persistent context only works with chromium engine

    if args.credentials_init:
        init_credentials_template(); return
    if args.credentials_check:
        check_credentials(); return
    if args.list_sessions:
        list_sessions(); return
    if args.capture_session:
        capture_session(args.capture_session); return
    if args.test_session:
        ok = test_session(args.test_session)
        sys.exit(0 if ok else 1)

    if args.from_keywords:
        for kw in INCLUDE_KEYWORDS:
            result = discover(kw)
            print_summary(result)
            if not args.no_report:
                write_discovery_report(result)
        return

    if not args.term:
        ap.print_help()
        sys.exit(1)

    result = discover(args.term)
    print_summary(result)
    if not args.no_report:
        write_discovery_report(result)


if __name__ == "__main__":
    main()
