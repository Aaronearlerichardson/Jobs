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

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from aiohttp import web

try:
    # typeshed types sys.stdout as TextIO, which has no reconfigure; the
    # console stream is a TextIOWrapper, and anything else raises here.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass

from src import runstate
from src import store
from src.crawl.page_capture import page_url, parse_page
from src.ops.ingest import ingest_external_jobs

PORT_DEFAULT = 8877


def _record_companies(conn: sqlite3.Connection, names: Iterable[str | None],
                      source_site: str, sites: dict[str, str] | None = None) -> list[str]:
    """Record captured company names as inactive store leads. `sites` maps a
    company name -> its own website (from JSON-LD hiringOrganization); stored as
    careers_url so `discover.py --resolve-leads` can probe {domain}/careers
    instead of guessing the domain from the name."""
    sites = sites or {}
    fresh = []
    have = {c["name"].lower() for c in store.get_companies(conn, active_only=False)}
    for n in sorted({n.strip() for n in names if n and n.strip()}):
        if n.lower() in have:
            continue
        row: dict[str, Any] = {"name": n, "active": 0, "source": "page_capture",
                               "notes": f"seen on {source_site}; resolve board via "
                                        f"discover.py --resolve-leads"}
        if sites.get(n):
            row["careers_url"] = sites[n]
        store.upsert_company(conn, row)
        fresh.append(n)
    return fresh


def attribute_company(conn: sqlite3.Connection, url: str,
                      jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The roster company that owns a captured page, or None -- and the jobs
    rewritten to carry its name, so the ingest links them to that row.

    A page saved from an employer's own careers host (or a hosted board the
    roster already knows by careers_url, or a posting whose JSON-LD names the
    employer's site) belongs to that employer; without this the parsed jobs
    would land under whatever the page text says and the roster would grow
    a second, unreviewed row for a company it already tracks. A matched row
    with no board of its own is marked capture-only (ats = "capture",
    active): a real roster member whose pages the person saves by hand, and
    one no crawl path will ever try to fetch. A row waiting in the review
    queue, or one that already has a board, is left as it is."""
    hints = [url] + [j.get("company_url") for j in jobs if j.get("company_url")]
    row = next((r for r in (store.company_by_host(conn, h) for h in hints if h)
                if r), None)
    if row is None:
        return None
    for j in jobs:
        j["company"] = row["name"]
    if not row.get("ats") and row["review"] != "pending":
        store.upsert_company(conn, {
            "name": row["name"], "ats": store.CAPTURE_ATS, "active": 1,
            "notes": "capture-only board: browse it yourself and save pages "
                     "with capture.py --watch"})
        row = store.get_company(conn, row["id"]) or row
    return row


async def ingest_html(url: str, html: str, label: str = "") -> dict[str, Any]:
    """Parse one page (off the loop) and feed the standard ingest pipeline.
    Returns a summary dict."""
    jobs, source, where = await asyncio.to_thread(
        lambda: (*parse_page(url, html), page_url(html, url)))
    async with store.Writer() as db:
        owner = await db.run(attribute_company, where, jobs)
        ingested = await ingest_external_jobs(jobs, source=source) if jobs else 0
        # Company name -> its own website, when the page exposed it (JSON-LD).
        sites = {j["company"]: j["company_url"] for j in jobs
                 if j.get("company") and j.get("company_url")}
        new_cos = await db.run(_record_companies, [j.get("company") for j in jobs],
                               source, sites)
    tag = label or url or source
    print(f"  {tag}: {len(jobs)} job(s) parsed, {ingested} ingested"
          + (f" under {owner['name']} ({owner.get('ats') or '?'})" if owner else "")
          + (f", {len(new_cos)} new compan(ies): {', '.join(new_cos[:6])}"
             + ("..." if len(new_cos) > 6 else "") if new_cos else ""))
    return {"parsed": len(jobs), "ingested": ingested, "companies": new_cos,
            "company": owner["name"] if owner else None}


async def serve(port: int) -> None:
    """The capture server on 127.0.0.1:`port`, until Ctrl+C: the
    userscript, its install page, and POST /page (a page's DOM, ingested),
    CORS-open for the userscript's cross-origin POST. No request size
    limit: a page's DOM runs to megabytes."""
    userscript = r"""// ==UserScript==
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

    index_html = """<!doctype html><meta charset="utf-8">
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

    async def answer(request: web.Request) -> web.Response:
        status, body, ctype = 200, index_html, "text/html"
        if request.method == "OPTIONS":
            status, body, ctype = 204, "", "application/json"
        elif request.method == "POST" and not request.path.startswith("/page"):
            status, body, ctype = 404, '{"error": "unknown endpoint"}', "application/json"
        elif request.method == "POST":
            ctype = "application/json"
            try:
                payload = json.loads((await request.read()).decode("utf-8", "replace"))
                body = json.dumps(await ingest_html(payload.get("url", ""),
                                                    payload.get("html", "")))
            except Exception as e:
                print(f"  [!] capture failed: {e}")
                status, body = 500, json.dumps({"error": str(e)})
        elif request.method != "GET":
            status, body, ctype = 501, "Unsupported method", "text/plain"
        elif request.path.startswith("/jobs-capture.user.js"):
            body, ctype = userscript.replace("__PORT__", str(port)), "text/javascript"
        return web.Response(status=status, text=body, content_type=ctype, charset="utf-8",
                            headers={"Access-Control-Allow-Origin": "*",
                                     "Access-Control-Allow-Headers": "Content-Type"})

    app = web.Application(client_max_size=0)
    app.router.add_route("*", "/{tail:.*}", answer)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
        print(f"\n  Jobs capture server on http://127.0.0.1:{port}/")
        print(f"  Userscript install: http://127.0.0.1:{port}/jobs-capture.user.js")
        print("  Ctrl+C to stop.\n")
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def _url_from_saved(html: str) -> str:
    """Source URL of a saved page: Chrome's 'saved from url' comment,
    SingleFile's banner, else the page's own canonical/og:url."""
    m = re.search(r"<!--\s*saved from url=\(\d+\)(\S+)", html) or \
        re.search(r"Page saved with SingleFile\s*\n\s*url:\s*(\S+)", html)
    return m.group(1) if m else ""      # parse_page falls back to canonical


async def watch(folder: str | Path, interval: float = 2.0) -> None:
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    print(f"\n  Watching {folder.resolve()}")
    print("  Save pages there (Ctrl+S -> 'Web Page, complete'). Ctrl+C to stop.\n")
    seen: dict[str, int] = {}
    while True:
        for p in sorted(folder.glob("*.htm*")):
            try:
                mtime = p.stat().st_mtime_ns
            except OSError:
                continue
            if seen.get(p.name) == mtime:
                continue
            await asyncio.sleep(0.6)    # let the browser finish writing
            try:
                html = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            seen[p.name] = mtime
            try:
                await ingest_html(_url_from_saved(html), html, label=p.name)
            except Exception as e:
                print(f"  [!] {p.name}: {e}")
        await asyncio.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="Manual page capture for the job crawler")
    ap.add_argument("files", nargs="*", help="Saved .html pages to ingest (Ctrl+S fallback)")
    ap.add_argument("--serve", action="store_true", help="Run the capture server (default when no files)")
    ap.add_argument("--watch", nargs="?", const="", metavar="FOLDER",
                    help="Watch FOLDER (default data/captures) and ingest every "
                         "page saved into it — no userscript manager needed")
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    ap.add_argument("--url", default="", help="Original page URL for a single ingested file "
                                              "(improves site-specific parsing)")
    # --add: hand-pick one job from a gated/JS site (Meta, Google, any custom
    # board) the auto-parsers can't reach. Bypasses the exclude/technical
    # gates (you chose it) but keeps the locality gate; also registers the
    # company and pulls its other local jobs when its board resolves.
    ap.add_argument("--add", action="store_true",
                    help="Add one curated job by fields (see --title/--company/--location)")
    ap.add_argument("--title", default="", help="With --add: the job title")
    ap.add_argument("--company", default="", help="With --add: the employer name")
    ap.add_argument("--location", default="", help="With --add: the job location (must be inside your "
                         "[locality] to pass the gate)")
    ap.add_argument("--desc", default="", help="With --add: optional job description")
    ap.add_argument("--no-board", action="store_true",
                    help="With --add: don't also pull the company's other jobs")
    args = ap.parse_args()

    if args.add:
        from src.ops.ingest import add_manual_job
        runstate.run(add_manual_job(url=args.url, title=args.title,
                                    company=args.company, location=args.location,
                                    description=args.desc,
                                    pull_board=not args.no_board))
        return

    if args.files:
        async def each() -> None:
            for f in args.files:
                p = Path(f)
                html = p.read_text(encoding="utf-8", errors="replace")
                await ingest_html(args.url or _url_from_saved(html), html, label=p.name)
        runstate.run(each())
        return
    try:
        if args.watch is not None:
            from src import config
            runstate.run(watch(args.watch or str(config.DATA_DIR / "captures")))
        else:
            runstate.run(serve(args.port))
    except KeyboardInterrupt:
        print("\n  stopped.")


if __name__ == "__main__":
    main()
