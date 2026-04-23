"""
Playwright session capture + saved-session reuse.

capture_session(): open a visible browser, let user log in manually,
    save the resulting storage_state (cookies + localStorage) to disk.
test_session():    re-open headless with saved state, verify it's valid.
list_sessions():   show captured sessions + age.
fetch_gated():     load a saved session and fetch a URL.

ToS: scraping authenticated LinkedIn/Indeed/etc. violates their ToS.
This tool reduces password-theft risk but not ToS risk. Use accordingly.
"""

import random
import time
from datetime import datetime
from pathlib import Path

from config import SESSION_DIR, SITE_CONFIGS
from .browser import open_context, require_browser, stealth_page


def _session_path(site):
    SESSION_DIR.mkdir(exist_ok=True)
    return SESSION_DIR / f"{site}.json"


def _jitter(min_ms=500, max_ms=1500):
    time.sleep(random.uniform(min_ms, max_ms) / 1000.0)


def _safe_goto(page, url, *, timeout_ms=20000):
    """
    Navigate, tolerating SPA client-side redirects that interrupt nav.
    Returns the final URL after settling, or None on real failure.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as e:
        msg = str(e)
        if "interrupted by another navigation" not in msg \
           and "Navigation timeout" not in msg:
            print(f"  [!] {url} navigation failed: {e}")
            return None
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    try:
        return page.url
    except Exception:
        return None


def _classify_url(final_url, cfg):
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


def _session_age_str(p):
    mtime = datetime.fromtimestamp(p.stat().st_mtime)
    delta = datetime.now() - mtime
    if delta.days >= 1:
        return f"{delta.days}d ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours}h ago"
    mins = delta.seconds // 60
    return f"{mins}m ago"


def capture_session(site):
    """Open visible browser, wait for manual login, save storage_state."""
    from . import runtime

    if site not in SITE_CONFIGS:
        print(f"  [!] Unknown site: {site!r}")
        print(f"      Known sites: {', '.join(SITE_CONFIGS)}")
        return None

    cfg = SITE_CONFIGS[site]
    open_pw = require_browser()
    out_path = _session_path(site)

    with open_pw() as pw:
        context, close_browser = open_context(pw, headless=False)
        page = context.new_page()
        stealth_page(page)

        mode = "real Chrome profile" if runtime.use_profile else runtime.browser_choice
        print(f"\n  Opening {site} login page in {mode}...")
        print(f"  Login URL: {cfg['login_url']}")

        try:
            page.goto(cfg["login_url"], wait_until="domcontentloaded")
        except Exception as e:
            if "interrupted by another navigation" not in str(e):
                print(f"  [!] Couldn't open login URL: {e}")
                close_browser()
                return None

        if runtime.use_profile:
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
            print(f"  + Login verified - landed at {final_url}")
        else:
            print(f"  ? Login unverified - landed at {final_url or '(unknown)'}.")
            print(f"    Saving anyway. Run --test-session {site} to confirm.")

        context.storage_state(path=str(out_path))
        close_browser()

    print(f"\n  Session saved -> {out_path}")
    print("  File is gitignored by default. Do NOT commit it.")
    print(f"  Verify with:  python discover.py --test-session {site}")
    return out_path


def test_session(site):
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
    open_pw = require_browser()

    print(f"  Loading saved session for {site}...")
    with open_pw() as pw:
        context, close_browser = open_context(
            pw, headless=True, storage_state=str(path))
        page = context.new_page()
        stealth_page(page)

        final_url = _safe_goto(page, cfg["verify_url"])
        _jitter()
        close_browser()

    verdict = _classify_url(final_url, cfg)
    age = _session_age_str(path)

    if verdict == "in":
        print(f"  + Session valid - landed at {final_url} (captured {age})")
        return True
    if verdict == "out":
        print(f"  x Session expired - bounced to {final_url}")
        print(f"    Re-run: python discover.py --capture-session {site} --use-profile")
        return False
    print(f"  ? Inconclusive - landed at {final_url or '(unknown)'}.")
    print(f"    Site may have changed URL conventions.")
    return False


def list_sessions():
    if not SESSION_DIR.exists():
        print("  No sessions captured yet.")
        print("  Run: python discover.py --capture-session linkedin")
        return
    files = sorted(SESSION_DIR.glob("*.json"))
    if not files:
        print("  No sessions captured yet.")
        return
    print(f"\n  Captured sessions - {SESSION_DIR}")
    for p in files:
        site = p.stem
        age = _session_age_str(p)
        kb = p.stat().st_size // 1024
        print(f"    - {site:<12} {age:<20} {kb} KB")
    print()


def fetch_gated(site, url, *, timeout_ms=20000):
    """
    Load a saved session and fetch a URL, returning HTML content.
    None on failure.  Callers should space requests generously
    (1 request / 5-15s with jitter, back off on 429/503).
    """
    path = _session_path(site)
    if not path.exists():
        print(f"  [!] No session for {site!r}. Run --capture-session first.")
        return None

    open_pw = require_browser()
    with open_pw() as pw:
        context, close_browser = open_context(
            pw, headless=True, storage_state=str(path))
        page = context.new_page()
        stealth_page(page)

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
