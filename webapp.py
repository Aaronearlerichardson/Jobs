#!/usr/bin/env python3
"""Local web UI for the job crawler.

    python webapp.py            ->  http://127.0.0.1:5533

One Flask process over the same modules the CLI uses: the ranked job list
(identical query to the digest), dispositions, the pipeline, the company
roster/watchlist, and every long-running operation (crawl, status sync,
deep-verify, closed-probe, rescore, backfills) run as a background thread
with its console output streamed to the browser. Single-user by design —
one operation at a time, same SQLite store as the CLI.
"""

import io
import json
import sys
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

import config
from jobcrawler import store
from jobcrawler.fetchers import company as company_fetch
from jobcrawler.tracks import local_tech

try:  # Windows consoles default to cp1252; job text carries em-dashes etc.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APP_DIR = Path(__file__).parent
app = Flask(__name__, static_folder=None)


def _conn():
    return store.connect()


def _today():
    return datetime.now().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
#  Background operation runner (one at a time, console tee'd to the browser)   #
# --------------------------------------------------------------------------- #

TASK = {"name": None, "thread": None, "log": [], "started": None,
        "ended": None, "error": None}
_LOG_LOCK = threading.Lock()


class _Tee(io.TextIOBase):
    """stdout tee: real console keeps printing; the browser polls the copy.
    Swapped in globally while an operation runs so the track modules' many
    worker-thread print()s are captured too."""

    def __init__(self, orig):
        self.orig = orig
        self._buf = ""

    def write(self, s):
        try:
            self.orig.write(s)
        except Exception:
            pass
        with _LOG_LOCK:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                TASK["log"].append(line)
                if len(TASK["log"]) > 5000:
                    del TASK["log"][:1000]
        return len(s)

    def flush(self):
        try:
            self.orig.flush()
        except Exception:
            pass


def _run_op(name, fn):
    def worker():
        orig = sys.stdout
        sys.stdout = _Tee(orig)
        try:
            fn()
        except Exception as e:
            TASK["error"] = f"{type(e).__name__}: {e}"
            print(f"  [!] operation failed: {TASK['error']}")
        finally:
            sys.stdout = orig
            TASK["ended"] = datetime.now().isoformat()
    with _LOG_LOCK:
        TASK["log"].clear()
    TASK.update(name=name, error=None, ended=None,
                started=datetime.now().isoformat())
    t = threading.Thread(target=worker, daemon=True)
    TASK["thread"] = t
    t.start()


def _running():
    t = TASK["thread"]
    return bool(t and t.is_alive())


def _int(p, key, default=None):
    v = str(p.get(key, "") or "").strip()
    return int(v) if v else default


OPS = {
    "crawl": {
        "label": "Crawl",
        "fn": lambda p: local_tech.run(max_workers=_int(p, "workers", 6),
                                       top_n=_int(p, "top", 15),
                                       verify=not p.get("no_verify")),
    },
    "sync": {
        "label": "Sync statuses",
        "fn": lambda p: local_tech.sync_status_all(top_n=_int(p, "top", 15)),
    },
    "verify": {
        "label": "Deep-verify top N",
        "fn": lambda p: local_tech.verify_top_cli(top_n=_int(p, "top", 15),
                                                  max_workers=4),
    },
    "check-closed": {
        "label": "Probe stale URLs",
        "fn": lambda p: local_tech.check_closed_jobs(
            stale_days=_int(p, "stale_days", 2), limit=_int(p, "limit")),
    },
    "rescore": {
        "label": "Rescore all",
        "fn": lambda p: local_tech.rescore_all(
            described_only=bool(p.get("described_only", True))),
    },
    "backfill-descriptions": {
        "label": "Backfill descriptions",
        "fn": lambda p: local_tech.backfill_board_descriptions(
            limit=_int(p, "limit")),
    },
    "backfill-workday": {
        "label": "Backfill Workday JDs",
        "fn": lambda p: __import__(
            "jobcrawler.fetchers.workday",
            fromlist=["backfill_workday_descriptions"]
        ).backfill_workday_descriptions(limit=_int(p, "limit")),
    },
    "nlx": {
        "label": "NLx ingest",
        "fn": lambda p: _op_nlx(p),
    },
    "discover-local": {
        "label": "Discover local companies",
        "fn": lambda p: __import__(
            "jobcrawler.discovery.local_sourcing",
            fromlist=["populate_companies"]
        ).populate_companies(dork=not p.get("no_dork")),
    },
    "dork": {
        "label": "ATS dork sweep",
        "fn": lambda p: __import__(
            "jobcrawler.discovery.ats_dork", fromlist=["run_ddgs_dorks"]
        ).run_ddgs_dorks(),
    },
    "score-missions": {
        "label": "Score missions",
        "fn": lambda p: __import__(
            "jobcrawler.discovery.local_sourcing",
            fromlist=["score_missions"]
        ).score_missions(rescore_all=bool(p.get("rescore"))),
    },
    "add-board": {
        "label": "Add company board",
        "fn": lambda p: __import__(
            "jobcrawler.discovery.local_sourcing", fromlist=["add_board"]
        ).add_board((p.get("name") or "").strip(), (p.get("url") or "").strip()),
    },
    "prune": {
        "label": "Prune dead boards",
        "fn": lambda p: _op_prune(p),
    },
    "dedup": {
        "label": "Dedup companies",
        "fn": lambda p: (lambda c: (store.dedup_companies(c), c.close()))(_conn()),
    },
    "add-job": {
        "label": "Add manual job",
        "fn": lambda p: local_tech.add_manual_job(
            url=p.get("url", ""), title=p.get("title", ""),
            company=p.get("company", ""), location=p.get("location", "")),
    },
}


def _op_nlx(p):
    """Pull NC postings for bot-gated employers (Meta/Google/Qualcomm...)
    from the federal NLx feed and run them through the local-tech ingest."""
    from jobcrawler.fetchers.careeronestop import fetch_nlx_company
    names = [n.strip() for n in (p.get("companies") or "").split(",") if n.strip()]
    if not names:
        print("  [!] give a comma-separated list of employer names")
        return
    total = 0
    for name in names:
        jobs = fetch_nlx_company(name)
        print(f"  {name}: {len(jobs)} NLx posting(s) in NC")
        if jobs:
            total += local_tech.ingest_external_jobs(jobs, source="nlx")
    print(f"  {total} new job(s) ingested from the NLx feed.")


def _op_prune(p):
    conn = _conn()
    try:
        store.prune_dead_boards(
            conn, deactivate_offmission=bool(p.get("offmission")))
    finally:
        conn.close()


@app.post("/api/run/<name>")
def api_run(name):
    if name not in OPS:
        return jsonify(error=f"unknown operation {name!r}"), 404
    if _running():
        return jsonify(error=f"'{TASK['name']}' is already running"), 409
    params = request.get_json(silent=True) or {}
    _run_op(name, lambda: OPS[name]["fn"](params))
    return jsonify(ok=True, name=name)


@app.get("/api/run/status")
def api_run_status():
    since = _int(request.args, "since", 0) or 0
    with _LOG_LOCK:
        lines = TASK["log"][since:]
        total = len(TASK["log"])
    return jsonify(running=_running(), name=TASK["name"],
                   started=TASK["started"], ended=TASK["ended"],
                   error=TASK["error"], lines=lines, total=total)


# --------------------------------------------------------------------------- #
#  Jobs / pipeline / companies / stats                                         #
# --------------------------------------------------------------------------- #

_JOB_FIELDS = (
    "job_id", "title", "company_name", "url", "location", "geo_mode",
    "resume_fit_score", "combined_score", "mission_tier", "mission_score",
    "fit_reason", "fit_gates", "fit_domain", "fit_function", "fit_stack",
    "fit_seniority", "posted_at", "first_seen", "last_seen", "status",
    "disposition", "disposition_note", "disposition_at",
)


def _job_json(r, today, rank=None):
    d = {k: r.get(k) for k in _JOB_FIELDS}
    d["rank"] = rank
    d["age"] = local_tech._age_tag(r, today)
    d["verified"] = "deep:" in (r.get("fit_reason") or "")
    return d


@app.get("/api/jobs")
def api_jobs():
    conn = _conn()
    # NC-locatable rows, plus remote rows at WATCHED companies only —
    # ranked_jobs enforces the watch check itself. The old unscoped remote
    # exception let slug-collision boards and overseas remotes into the
    # local list (see tracks/local_tech.py run()).
    rows = store.ranked_jobs(
        conn, track=local_tech.TRACK, location_re=company_fetch.NC_RE,
        rank_by="fit", allow_geo_modes={"remote"},
        min_mission=local_tech.MIN_MISSION_FOR_RANKING,
        include_closed=request.args.get("closed") == "1",
        include_dispositioned=request.args.get("dispositioned") == "1")
    conn.close()
    today = _today()
    return jsonify([_job_json(r, today, i + 1) for i, r in enumerate(rows)])


@app.get("/api/job/<job_id>")
def api_job(job_id):
    conn = _conn()
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify(error="not found"), 404
    d = dict(row)
    d["age"] = local_tech._age_tag(d, _today())
    return jsonify(d)


@app.post("/api/job/<job_id>/disposition")
def api_disposition(job_id):
    p = request.get_json(silent=True) or {}
    conn = _conn()
    row, err = store.set_disposition(conn, job_id, p.get("disposition", ""),
                                     note=(p.get("note") or "").strip() or None)
    conn.close()
    if err:
        return jsonify(error=err), 400
    return jsonify(ok=True, job_id=row["job_id"])


@app.get("/api/pipeline")
def api_pipeline():
    conn = _conn()
    rows = store.get_pipeline(conn)
    conn.close()
    today = _today()
    return jsonify([_job_json(r, today) for r in rows])


@app.get("/api/companies")
def api_companies():
    conn = _conn()
    comps = store.get_companies(conn, active_only=False)
    counts = dict(conn.execute(
        "SELECT company_id, COUNT(*) FROM jobs "
        "WHERE COALESCE(status,'open')!='closed' AND company_id IS NOT NULL "
        "GROUP BY company_id").fetchall())
    conn.close()
    out = []
    for c in comps:
        tags = {t for t in (c.get("tags") or "").split(",") if t}
        out.append({
            "id": c["id"], "name": c["name"], "ats": c.get("ats"),
            "mission_tier": c.get("mission_tier"),
            "mission_score": c.get("mission_score"),
            "active": bool(c.get("active")), "tags": sorted(tags),
            "watched": "watch" in tags, "open_jobs": counts.get(c["id"], 0),
        })
    return jsonify(out)


@app.post("/api/company/<int:cid>/watch")
def api_watch(cid):
    on = bool((request.get_json(silent=True) or {}).get("on"))
    conn = _conn()
    row = conn.execute("SELECT name FROM companies WHERE id=?", (cid,)).fetchone()
    if not row:
        conn.close()
        return jsonify(error="not found"), 404
    store.set_company_tag(conn, row["name"], "watch", add=on)
    conn.close()
    return jsonify(ok=True, watched=on)


@app.post("/api/company/<int:cid>/active")
def api_active(cid):
    on = 1 if (request.get_json(silent=True) or {}).get("on") else 0
    conn = _conn()
    conn.execute("UPDATE companies SET active=? WHERE id=?", (on, cid))
    conn.commit()
    conn.close()
    return jsonify(ok=True, active=bool(on))


@app.post("/api/import/companies")
def api_import():
    """Upsert companies from an exported roster JSON (idempotent — tags
    merge, existing mission scores survive None fields)."""
    f = request.files.get("file")
    if not f:
        return jsonify(error="no file uploaded"), 400
    import tempfile
    with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as t:
        f.save(t)
        tmp = t.name
    try:
        conn = _conn()
        n = store.import_companies(conn, tmp)
        conn.close()
    except Exception as e:
        return jsonify(error=f"import failed: {e}"), 400
    finally:
        try:
            Path(tmp).unlink()
        except OSError:
            pass
    return jsonify(ok=True, imported=n)


@app.get("/api/export/companies")
def api_export():
    conn = _conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM companies ORDER BY name").fetchall()]
    conn.close()
    for r in rows:
        r.pop("id", None)
    buf = io.BytesIO(json.dumps(rows, indent=1, ensure_ascii=False).encode("utf-8"))
    return send_file(buf, mimetype="application/json", as_attachment=True,
                     download_name="company_roster.json")


@app.get("/api/stats")
def api_stats():
    conn = _conn()

    def one(q, args=()):
        return conn.execute(q, args).fetchone()[0]

    today = _today()
    stats = {
        "open": one("SELECT COUNT(*) FROM jobs WHERE COALESCE(status,'open')!='closed'"),
        "closed": one("SELECT COUNT(*) FROM jobs WHERE status='closed'"),
        "new_today": one("SELECT COUNT(*) FROM jobs WHERE substr(first_seen,1,10)=? "
                         "AND COALESCE(status,'open')!='closed'", (today,)),
        "dated": one("SELECT COUNT(posted_at) FROM jobs "
                     "WHERE COALESCE(status,'open')!='closed'"),
        "pipeline": one("SELECT COUNT(*) FROM jobs "
                        "WHERE disposition IN ('applied','interviewing')"),
        "saved": one("SELECT COUNT(*) FROM jobs WHERE disposition='saved'"),
        "companies_active": one("SELECT COUNT(*) FROM companies WHERE active=1"),
        "watched": one("SELECT COUNT(*) FROM companies WHERE "
                       "(','||COALESCE(tags,'')||',') LIKE '%,watch,%'"),
        "api_key": config.ANTHROPIC_API_KEY != "YOUR_ANTHROPIC_API_KEY_HERE",
        "screen_model": config.CLAUDE_MODEL,
        "verify_model": config.CLAUDE_VERIFY_MODEL,
        "db": str(config.STORE_DB_PATH),
    }
    conn.close()
    return jsonify(stats)


@app.get("/")
def index():
    return send_from_directory(APP_DIR / "webui", "index.html")


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


def main():
    """Start the UI. Flags: --port=N (default 5533, or WEBUI_PORT env),
    --open (launch the default browser once the server is up — the default
    when running as a compiled executable), --no-open.

    Launch is idempotent: if this app is already running on the port, the
    new process just opens a browser tab to it and exits instead of piling
    a second server onto the same socket. If something ELSE holds the port,
    the next free one (up to +10) is used."""
    import os
    import webbrowser

    port = int(os.environ.get("WEBUI_PORT", "5533"))
    for a in sys.argv[1:]:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    compiled = "__compiled__" in globals()
    auto_open = ("--no-open" not in sys.argv
                 and (compiled or "--open" in sys.argv))

    if _ours_on(port):
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

    url = f"http://127.0.0.1:{port}"
    print(f"  job-crawler UI -> {url}")
    print(f"  db: {config.STORE_DB_PATH}")
    if config.ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_API_KEY_HERE":
        print("  [!] ANTHROPIC_API_KEY not set - scoring operations will no-op.")
    print("  Ctrl+C (or close this window) to stop.")
    if auto_open:
        _open_when_up(url, port)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code:
            print(e.code if isinstance(e.code, str) else f"exit {e.code}")
    except Exception as e:
        print(f"\n  [!] failed to start: {type(e).__name__}: {e}")
        # Double-clicked console windows vanish on exit — hold them open so
        # the error is actually readable.
        try:
            if sys.stdin and sys.stdin.isatty():
                input("  Press Enter to close...")
        except EOFError:
            pass
