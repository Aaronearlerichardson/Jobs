"""Server lifecycle: port selection, idempotent launch, the graceful
self-restart used by the Settings tab, and main()."""

import os
import subprocess
import sys
import threading

import config

from . import STATE, app


def _ours_on(port):
    """True if a RUNNING instance of this app already serves `port`."""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/stats", timeout=2) as r:
            return b"screen_model" in r.read(4096)
    except Exception:
        return False


def _port_free(port):
    """Exclusive-bind probe. Windows quietly lets several servers bind the
    SAME port when SO_REUSEADDR is involved (Werkzeug sets it), and then
    delivers connections to an arbitrary one — the browser sees random
    connection failures instead of a clean 'address in use' error. A plain
    test bind (no reuse flags) reliably reports occupancy first."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _open_when_up(url, port, timeout=25.0):
    """Open the browser only once the server actually accepts connections
    (a fixed delay races antivirus-slowed first launches of the exe)."""
    import socket
    import time
    import webbrowser

    def waiter():
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                webbrowser.open(url)
                return
            except OSError:
                time.sleep(0.3)
    threading.Thread(target=waiter, daemon=True).start()


def schedule_restart():
    """Spawn a detached successor process on the same port and exit. The
    successor runs --takeover (waits for this process's socket to free up
    instead of bailing out on the already-running probe). Works for both
    `python webapp.py` and the Nuitka exe (sys.argv[0] is the exe)."""
    STATE["restarting"] = True

    def worker():
        import time
        time.sleep(0.75)          # let the HTTP response flush to the browser
        if "__compiled__" in globals():
            cmd = [sys.argv[0]]
        else:
            cmd = [sys.executable, str(config.SCRIPT_DIR / "webapp.py")]
        cmd += [f"--port={STATE['bound_port']}", "--no-open", "--takeover"]
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(cmd, cwd=str(config.SCRIPT_DIR),
                         close_fds=True, creationflags=flags)
        os._exit(0)

    threading.Thread(target=worker, daemon=True).start()


def main():
    """Start the UI. Flags: --port=N (default 5533, or WEBUI_PORT env),
    --open (launch the default browser once the server is up — the default
    when running as a compiled executable), --no-open, --takeover (restart
    successor: wait for the predecessor's socket instead of bailing out).

    Launch is idempotent: if this app is already running on the port, the
    new process just opens a browser tab to it and exits instead of piling
    a second server onto the same socket. If something ELSE holds the port,
    the next free one (up to +10) is used."""
    import webbrowser

    port = int(os.environ.get("WEBUI_PORT", "5533"))
    for a in sys.argv[1:]:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    compiled = "__compiled__" in globals()
    auto_open = ("--no-open" not in sys.argv
                 and (compiled or "--open" in sys.argv))

    if "--takeover" in sys.argv:
        # Config-save restart successor: our dying predecessor still holds
        # the port for a moment. Wait for it instead of the idempotent
        # "already running" bail-out — we ARE the replacement.
        import time
        deadline = time.time() + 20
        while time.time() < deadline:
            if _port_free(port):
                break
            time.sleep(0.25)
        else:
            raise SystemExit(f"  [!] restart takeover timed out - port {port} "
                             "still busy after 20s. Start the UI manually.")
    elif _ours_on(port):
        url = f"http://127.0.0.1:{port}"
        print(f"  already running -> {url}  (opening browser; this window can close)")
        if "--no-open" not in sys.argv:
            webbrowser.open(url)
        return
    if not _port_free(port):
        for cand in range(port + 1, port + 11):
            if _ours_on(cand):
                url = f"http://127.0.0.1:{cand}"
                print(f"  already running -> {url}  (opening browser)")
                if "--no-open" not in sys.argv:
                    webbrowser.open(url)
                return
            if _port_free(cand):
                print(f"  [!] port {port} is in use by another program - "
                      f"using {cand} instead")
                port = cand
                break
        else:
            raise SystemExit(f"  [!] no free port in {port}..{port + 10}")

    from core import bootstrap
    bootstrap.ensure_profile()

    STATE["bound_port"] = port
    url = f"http://127.0.0.1:{port}"
    print(f"  job-crawler UI -> {url}")
    for line in bootstrap.status_lines():
        print(f"  {line}")
    print(f"  db      : {config.STORE_DB_PATH}")
    if config.ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_API_KEY_HERE":
        print("  [!] ANTHROPIC_API_KEY not set - scoring operations will no-op.")
    print("  Ctrl+C (or close this window) to stop.")
    if auto_open:
        _open_when_up(url, port)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
