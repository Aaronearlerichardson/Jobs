"""
Browser launching + stealth patching.

Handles Playwright's two stealth-library API versions (v1 and v2),
strips the tell-tale automation flags that Cloudflare fingerprints,
and dispatches between chrome / chromium / firefox engines.
"""

import sys

from config import BROWSER_UA


# Strip Playwright's default automation-advertisements:
#   --enable-automation : banner + navigator.webdriver = true
#   --no-sandbox        : "unsupported command-line flag" banner
#   --disable-extensions: kills profile extensions (trust signals)
CHROMIUM_DEANON_ARGS = {
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


def require_browser():
    """
    Lazy-import playwright + playwright-stealth. Returns a factory that
    opens a sync_playwright() context (v2) or a pre-stealthed one (v1).

    Handles both:
      * v2.x: Stealth().use_sync(sync_playwright())   [preferred]
      * v1.x: sync_playwright(); caller must stealth_sync(page) later.
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

    # v2 API
    try:
        from playwright_stealth import Stealth
        return lambda: Stealth().use_sync(sync_playwright())
    except ImportError:
        pass

    # v1 API
    try:
        from playwright_stealth import stealth_sync  # noqa: F401
        return sync_playwright
    except ImportError:
        print(
            "\n  [!] playwright-stealth is not installed.\n"
            "  Install with:\n"
            "      pip install playwright-stealth\n"
        )
        sys.exit(1)


def stealth_page(page):
    """No-op for v2 (use_sync already did it); applies stealth_sync for v1."""
    try:
        from playwright_stealth import Stealth  # noqa: F401
        return
    except ImportError:
        pass
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except ImportError:
        pass


def launch_browser(pw, choice, *, headless):
    """Launch the chosen engine. Returns a Browser instance."""
    if choice == "firefox":
        return pw.firefox.launch(headless=headless)
    if choice == "chrome":
        try:
            return pw.chromium.launch(
                headless=headless, channel="chrome",
                **CHROMIUM_DEANON_ARGS,
            )
        except Exception as e:
            print(f"  [!] Couldn't launch real Chrome ({e}).")
            print(f"      Install with:  playwright install chrome")
            print(f"      Falling back to bundled Chromium (Cloudflare may block).")
            return pw.chromium.launch(headless=headless, **CHROMIUM_DEANON_ARGS)
    return pw.chromium.launch(headless=headless, **CHROMIUM_DEANON_ARGS)


def open_context(pw, *, headless, storage_state=None):
    """
    Returns (context, closer). Two modes:
      * ephemeral (default): launch Browser -> new_context()
      * persistent (--use-profile): launch_persistent_context against a
        COPY of the user's real Chrome profile. Storage_state is ignored.
    """
    from . import runtime
    from .profile import (
        clear_chrome_locks,
        prepare_profile_copy,
        reset_chrome_session_state,
    )

    if runtime.use_profile:
        user_dir = prepare_profile_copy(refresh=runtime.refresh_profile)
        clear_chrome_locks(user_dir)
        reset_chrome_session_state(user_dir, runtime.profile_directory)

        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=user_dir,
                headless=headless,
                channel="chrome",
                ignore_default_args=CHROMIUM_DEANON_ARGS["ignore_default_args"],
                args=[
                    f"--profile-directory={runtime.profile_directory}",
                    *CHROMIUM_DEANON_ARGS["args"],
                ],
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
                # No user_agent override — the profile brings its own.
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

    browser = launch_browser(pw, runtime.browser_choice, headless=headless)
    context = browser.new_context(
        user_agent=BROWSER_UA,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        storage_state=storage_state,
    )
    return context, browser.close
