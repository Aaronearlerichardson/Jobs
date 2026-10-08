"""Server lifecycle: port selection, idempotent launch, the graceful
self-restart used by the Settings tab, main(), and the event loop the
request threads reach async code through (`call`)."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Coroutine
from http.client import HTTPException
from typing import Never

from src import config, runstate
from src.claude.api import have_api_key
from src.config import bootstrap
from . import STATE, app

_log = logging.getLogger(__name__)

#: The web UI's event loop, on a daemon thread of its own, once started.
_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_START = threading.Lock()


def call[T](coro: Coroutine[object, Never, T]) -> T:
    """`coro`'s result, run on the web UI's event loop while this request
    thread waits, as a run of its own (src/runstate.py): the one way a
    thread reaches async code. The loop starts at first use; at exit, what
    still runs on it is cancelled and unwinds (`_unwind`).

    On the loop itself it could only deadlock, so it refuses:

    >>> async def nested():
    ...     return call(asyncio.sleep(0))
    >>> call(nested())
    Traceback (most recent call last):
    RuntimeError: call on the web UI's loop: await the coroutine instead
    """
    global _LOOP
    try:
        on_loop = _LOOP is not None and asyncio.get_running_loop() is _LOOP
    except RuntimeError:                 # no loop runs in this thread
        on_loop = False
    if on_loop:
        coro.close()
        raise RuntimeError("call on the web UI's loop: await the coroutine instead")
    with _LOOP_START:
        if _LOOP is None:
            _LOOP = asyncio.new_event_loop()
            threading.Thread(target=_LOOP.run_forever, name="web-loop",
                             daemon=True).start()
            atexit.register(_unwind, _LOOP)

    async def in_run() -> T:
        async with runstate.Run():
            return await coro
    return asyncio.run_coroutine_threadsafe(in_run(), _LOOP).result()


def _unwind(loop: asyncio.AbstractEventLoop) -> None:
    """At exit: cancel `loop`'s tasks (a running op) and give them 10 s to
    unwind, so an open store batch rolls back and a run closes its
    session, then stop the loop."""
    async def cancel_all() -> None:
        tasks = asyncio.all_tasks() - {asyncio.current_task()}
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=10)
    try:
        asyncio.run_coroutine_threadsafe(cancel_all(), loop).result(timeout=12)
    except (TimeoutError, RuntimeError) as e:
        _log.debug("unwind at exit: %s", type(e).__name__)
    loop.call_soon_threadsafe(loop.stop)


def _ours_on(port: int) -> bool:
    """True if a RUNNING instance of this app already serves `port`."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/stats", timeout=2) as r:
            head: bytes = r.read(4096)
            return b"screen_model" in head
    except (OSError, HTTPException) as e:
        _log.debug("port %s not ours: %s", port, type(e).__name__)
        return False


def _port_free(port: int) -> bool:
    """Exclusive-bind probe. Windows quietly lets several servers bind the
    SAME port when SO_REUSEADDR is involved (Werkzeug sets it), and then
    delivers connections to an arbitrary one — the browser sees random
    connection failures instead of a clean 'address in use' error. A plain
    test bind (no reuse flags) reliably reports occupancy first."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _open_when_up(url: str, port: int, timeout: float = 25.0) -> None:
    """Open the browser only once the server actually accepts connections
    (a fixed delay races antivirus-slowed first launches of the exe)."""

    def waiter() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                webbrowser.open(url)
                return
            except OSError:
                time.sleep(0.3)
    threading.Thread(target=waiter, daemon=True).start()


def schedule_restart() -> None:
    """Spawn a detached successor process on the same port and exit. The
    successor runs --takeover (waits for this process's socket to free up
    instead of bailing out on the already-running probe). Works for both
    `python webapp.py` and the Nuitka exe (sys.argv[0] is the exe)."""
    STATE["restarting"] = True

    def worker() -> None:
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


def main() -> None:
    """Start the UI. Flags: --port=N (default 5533, or WEBUI_PORT env),
    --open (launch the default browser once the server is up — the default
    when running as a compiled executable), --no-open, --takeover (restart
    successor: wait for the predecessor's socket instead of bailing out).

    Launch is idempotent: if this app is already running on the port, the
    new process just opens a browser tab to it and exits instead of piling
    a second server onto the same socket. If something ELSE holds the port,
    the next free one (up to +10) is used."""
    port = config.SETTINGS.webui_port
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

    bootstrap.ensure_profile()

    STATE["bound_port"] = port
    url = f"http://127.0.0.1:{port}"
    print(f"  job-crawler UI -> {url}")
    for line in bootstrap.status_lines():
        print(f"  {line}")
    print(f"  db      : {config.STORE_DB_PATH}")
    if not have_api_key():
        print("  [!] ANTHROPIC_API_KEY not set - scoring operations will no-op.")
    print("  Ctrl+C (or close this window) to stop.")
    if auto_open:
        _open_when_up(url, port)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
