#!/usr/bin/env python3
"""Manual page capture: browse job boards yourself, send pages to the crawler.

Two ways in, one pipeline:

  python capture.py                     # start the local capture server
      Then click the userscript button on a LinkedIn/Indeed page (install:
      open http://127.0.0.1:8877/ in the browser, one-time). Each click
      POSTs the live DOM here; jobs are parsed, gated (exclude + technical
      title), resume-fit-scored, and written to the store.

  python capture.py page1.html [...]    # ingest pages saved with Ctrl+S
      Same pipeline for saved files (works even where the userscript can't).

  python capture.py --watch [FOLDER]    # no userscript manager needed
      Watches a folder (default: ./captures) and ingests every page you
      save into it. Flow: browse, Ctrl+S ("Web Page, complete"), Enter —
      the watcher picks it up within ~2s. Firefox remembers the folder.

Companies seen on captured pages are recorded in the store (inactive, source
'page_capture') so a later `python discover.py --local` pass can resolve
their boards. No automated fetching of logged-in sites happens here — you
drive the browser; this just keeps what you saw.
"""

import argparse
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from jobcrawler import store
from jobcrawler.page_capture import parse_page
from jobcrawler.tracks.local_tech import ingest_external_jobs

PORT_DEFAULT = 8877

USERSCRIPT = r"""// ==UserScript==
// @name         Jobs capture button
// @namespace    jobs-crawler
// @version      1.0
// @description  Send the current job-board page to the local Jobs crawler
// @match        https://www.linkedin.com/*
// @match        https://www.indeed.com/*
// @match        https://wellfound.com/*
// @match        https://*.builtin.com/*
// @match        https://www.metacareers.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @connect      127.0.0.1
// ==/UserScript==
(function () {
  const btn = document.createElement("button");
  btn.textContent = "➤ Jobs";
  Object.assign(btn.style, {
    position: "fixed", bottom: "18px", right: "18px", zIndex: 2147483647,
    padding: "10px 14px", borderRadius: "8px", border: "none",
    background: "#0a66c2", color: "#fff", font: "bold 13px sans-serif",
    cursor: "pointer", boxShadow: "0 2px 8px rgba(0,0,0,.35)",
  });
  const flash = (msg, ok) => {
    btn.textContent = msg;
    btn.style.background = ok ? "#1c8c3c" : "#b3261e";
    setTimeout(() => { btn.textContent = "➤ Jobs"; btn.style.background = "#0a66c2"; }, 3500);
  };
  btn.addEventListener("click", () => {
    btn.textContent = "…";
    const gmx = (typeof GM_xmlhttpRequest !== "undefined") ? GM_xmlhttpRequest
              : (typeof GM !== "undefined" && GM.xmlHttpRequest);
    gmx({
      method: "POST",
      url: "http://127.0.0.1:__PORT__/page",
      headers: { "Content-Type": "text/plain" },
      data: JSON.stringify({ url: location.href,
                             html: document.documentElement.outerHTML }),
      onload: (r) => {
        try {
          const d = JSON.parse(r.responseText);
          flash(`✓ ${d.parsed} job(s), ${d.ingested} new`, true);
        } catch (e) { flash("✕ bad reply", false); }
      },
      onerror: () => flash("✕ server off?", false),
    });
  });
  document.documentElement.appendChild(btn);
})();
"""

INDEX_HTML = """<!doctype html><meta charset="utf-8">
<title>Jobs capture server</title>
<body style="font-family:sans-serif;max-width:640px;margin:40px auto">
<h2>Jobs capture server &mdash; running</h2>
<ol>
  <li>Install a userscript manager (Violentmonkey / Tampermonkey).</li>
  <li><a href="/jobs-capture.user.js">Install the capture userscript</a>
      (the manager will prompt).</li>
  <li>Browse LinkedIn / Indeed logged in as yourself; click the
      <b>&#10148; Jobs</b> button on any results or job page.</li>
</ol>
<p>Fallback without a userscript: save pages with Ctrl+S and run<br>
<code>python capture.py saved-page.html</code></p>
</body>"""


def _record_companies(names, source_site):
    """Record captured company names as inactive store leads."""
    fresh = []
    conn = store.connect()
    have = {c["name"].lower() for c in store.get_companies(conn, active_only=False)}
    for n in sorted({n.strip() for n in names if n and n.strip()}):
        if n.lower() in have:
            continue
        store.upsert_company(conn, {
            "name": n, "active": 0, "source": "page_capture",
            "notes": f"seen on {source_site}; resolve board via discover.py --local",
        })
        fresh.append(n)
    conn.close()
    return fresh


def ingest_html(url, html, label=""):
    """Parse one page and feed the standard ingest pipeline. Returns a
    summary dict."""
    jobs, source = parse_page(url, html)
    ingested = ingest_external_jobs(jobs, source=source) if jobs else 0
    new_cos = _record_companies((j.get("company") for j in jobs), source)
    tag = label or url or source
    print(f"  {tag}: {len(jobs)} job(s) parsed, {ingested} ingested"
          + (f", {len(new_cos)} new compan(ies): {', '.join(new_cos[:6])}"
             + ("..." if len(new_cos) > 6 else "") if new_cos else ""))
    return {"parsed": len(jobs), "ingested": ingested, "companies": new_cos}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, "")

    def do_GET(self):
        if self.path.startswith("/jobs-capture.user.js"):
            self._send(200, USERSCRIPT.replace("__PORT__", str(self.server.server_port)),
                       ctype="text/javascript")
        else:
            self._send(200, INDEX_HTML, ctype="text/html")

    def do_POST(self):
        if not self.path.startswith("/page"):
            self._send(404, '{"error": "unknown endpoint"}')
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            summary = ingest_html(payload.get("url", ""), payload.get("html", ""))
            self._send(200, json.dumps(summary))
        except Exception as e:
            print(f"  [!] capture failed: {e}")
            self._send(500, json.dumps({"error": str(e)}))

    def log_message(self, *a):        # quiet the default per-request noise
        pass


def serve(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"\n  Jobs capture server on http://127.0.0.1:{port}/")
    print(f"  Userscript install: http://127.0.0.1:{port}/jobs-capture.user.js")
    print("  Ctrl+C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")


def _url_from_saved(html):
    """Source URL of a saved page: Chrome's 'saved from url' comment,
    SingleFile's banner, else the page's own canonical/og:url."""
    m = re.search(r"<!--\s*saved from url=\(\d+\)(\S+)", html) or \
        re.search(r"Page saved with SingleFile\s*\n\s*url:\s*(\S+)", html)
    return m.group(1) if m else ""      # parse_page falls back to canonical


def watch(folder, interval=2.0):
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    print(f"\n  Watching {folder.resolve()}")
    print("  Save pages there (Ctrl+S -> 'Web Page, complete'). Ctrl+C to stop.\n")
    seen = {}
    while True:
        for p in sorted(folder.glob("*.htm*")):
            try:
                mtime = p.stat().st_mtime_ns
            except OSError:
                continue
            if seen.get(p.name) == mtime:
                continue
            time.sleep(0.6)             # let the browser finish writing
            try:
                html = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            seen[p.name] = mtime
            try:
                ingest_html(_url_from_saved(html), html, label=p.name)
            except Exception as e:
                print(f"  [!] {p.name}: {e}")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n  stopped.")
            return


def main():
    ap = argparse.ArgumentParser(description="Manual page capture for the job crawler")
    ap.add_argument("files", nargs="*", help="Saved .html pages to ingest (Ctrl+S fallback)")
    ap.add_argument("--serve", action="store_true", help="Run the capture server (default when no files)")
    ap.add_argument("--watch", nargs="?", const="captures", metavar="FOLDER",
                    help="Watch FOLDER (default ./captures) and ingest every "
                         "page saved into it — no userscript manager needed")
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    ap.add_argument("--url", default="", help="Original page URL for a single ingested file "
                                              "(improves site-specific parsing)")
    # --add: hand-pick one job from a gated/JS site (Meta, Google, any custom
    # board) the auto-parsers can't reach. Bypasses the exclude/technical
    # gates (you chose it) but keeps the NC location gate; also registers the
    # company and pulls its other NC jobs when its board resolves.
    ap.add_argument("--add", action="store_true",
                    help="Add one curated job by fields (see --title/--company/--location)")
    ap.add_argument("--title", default="", help="With --add: the job title")
    ap.add_argument("--company", default="", help="With --add: the employer name")
    ap.add_argument("--location", default="", help="With --add: the job location (must be NC to pass the gate)")
    ap.add_argument("--desc", default="", help="With --add: optional job description")
    ap.add_argument("--no-board", action="store_true",
                    help="With --add: don't also pull the company's other jobs")
    args = ap.parse_args()

    if args.add:
        from jobcrawler.tracks.local_tech import add_manual_job
        add_manual_job(url=args.url, title=args.title, company=args.company,
                       location=args.location, description=args.desc,
                       pull_board=not args.no_board)
        return

    if args.files:
        for f in args.files:
            p = Path(f)
            html = p.read_text(encoding="utf-8", errors="replace")
            ingest_html(args.url or _url_from_saved(html), html, label=p.name)
        return
    if args.watch:
        watch(args.watch)
        return
    serve(args.port)


if __name__ == "__main__":
    main()
