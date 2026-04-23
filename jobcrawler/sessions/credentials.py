"""
Legacy cookie-paste credentials manager.

Kept for users who want to paste cookies (e.g. li_at from DevTools).
Session capture (--capture-session) is preferred — no password ever
touches disk and 2FA/CAPTCHA is handled by the human.
"""

import json

from config import CREDENTIALS_PATH, CREDENTIALS_TEMPLATE_PATH, BROWSER_UA


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
        "_how": "Log into linkedin.com in a browser, open DevTools -> "
                "Application -> Cookies -> copy the value of 'li_at'.",
        "li_at": "",
        "user_agent": BROWSER_UA,
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
        "_how": "Catch-all for any other site. Fetcher reads these keys by name.",
    },
}


def init_credentials_template():
    if CREDENTIALS_TEMPLATE_PATH.exists():
        print(f"  Template already exists: {CREDENTIALS_TEMPLATE_PATH}")
    else:
        CREDENTIALS_TEMPLATE_PATH.write_text(
            json.dumps(CREDENTIAL_SCHEMA, indent=2), encoding="utf-8")
        print(f"  Wrote template -> {CREDENTIALS_TEMPLATE_PATH}")

    if CREDENTIALS_PATH.exists():
        print(f"  credentials.json already exists - not overwriting.")
    else:
        CREDENTIALS_PATH.write_text(
            json.dumps(CREDENTIAL_SCHEMA, indent=2), encoding="utf-8")
        print(f"  Scaffold -> {CREDENTIALS_PATH}")
        print(f"  Edit this file, then confirm it's in .gitignore "
              f"before committing anything.")
    return CREDENTIALS_PATH


def load_credentials():
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [!] credentials.json is malformed: {e}")
        return {}


def get_credentials(site):
    return load_credentials().get(site, {}) or {}


def check_credentials():
    creds = load_credentials()
    if not creds:
        print("  No credentials.json found. Run: python discover.py --credentials-init")
        return
    print(f"\n  credentials.json - {CREDENTIALS_PATH}")
    for site, block in creds.items():
        if site.startswith("_"):
            continue
        if not isinstance(block, dict):
            continue
        filled = [k for k, v in block.items()
                  if not k.startswith("_") and isinstance(v, str) and v]
        status = f"{len(filled)} value(s) set" if filled else "empty"
        print(f"    - {site:<12} {status}")
    print()
