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
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

import config
from jobcrawler import nc, profile_edit, remote_filter, store
from jobcrawler.fetchers import company as company_fetch
from jobcrawler.tracks import local_tech

try:  # Windows consoles default to cp1252; job text carries em-dashes etc.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APP_DIR = Path(__file__).parent
app = Flask(__name__, static_folder=None)

# Changes on every process start; the restart overlay polls /api/stats until
# this differs from the value it remembered, i.e. the successor is up.
BOOT_ID = uuid.uuid4().hex

# The port this instance actually bound (set in main(); the restart successor
# must inherit it). Falls back to the default for `flask run`-style launches.
BOUND_PORT = int(os.environ.get("WEBUI_PORT", "5533"))

RESTARTING = False


def _track():
    """Resolve the request's track config ([tracks.*] in profile.toml).
    Accepts ?track=<id> (or a "track" key in a JSON body); unknown ids fall
    back to the default track rather than erroring — a stale localStorage
    value after a config edit shouldn't brick the UI."""
    tid = (request.args.get("track")
           or (request.get_json(silent=True) or {}).get("track")
           or config.DEFAULT_TRACK)
    return config.UI_TRACKS.get(tid) or config.UI_TRACKS[config.DEFAULT_TRACK]


def _conn(track_cfg=None):
    return store.connect(track_cfg["db_path"] if track_cfg else None)


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


# Pristine keyword lists, captured before any track's apply_to_config()
# mutates them. Both tracks now run in THIS process: local_tech extends the
# shared lists, remote_neural REPLACES them — without a reset between ops,
# running one track's crawl would poison the next one's keyword filter.
_BASELINE_KW = (list(config.CORE_KEYWORDS), list(config.DOMAIN_KEYWORDS),
                list(config.SKILL_KEYWORDS), list(config.INCLUDE_KEYWORDS),
                bool(getattr(config, "ACCEPT_REMOTE", False)))


def _restore_keywords():
    """Reset config's shared keyword lists (in place, so modules holding
    references see it) to their import-time state."""
    core, dom, skill, inc, accept = _BASELINE_KW
    config.CORE_KEYWORDS[:] = core
    config.DOMAIN_KEYWORDS[:] = dom
    config.SKILL_KEYWORDS[:] = skill
    config.INCLUDE_KEYWORDS[:] = inc
    config.ACCEPT_REMOTE = accept


def _run_op(name, fn):
    def worker():
        orig = sys.stdout
        sys.stdout = _Tee(orig)
        try:
            _restore_keywords()
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


# Each op declares which crawl ENGINE it needs ("local" = the location-scoped
# crawler in tracks/local_tech.py, "neural" = the location-agnostic runner,
# None = engine-agnostic store maintenance that runs against whichever
# track's DB is active). Ops are matched to the active track by its
# profile-configured `engine` — never by the user-chosen track id.
OPS = {
    "crawl": {
        "label": "Crawl",
        "engine": None,   # one command for every track — dispatches on engine
        "fn": lambda p: _op_crawl(p),
    },
    "sync": {
        "label": "Sync statuses",
        "engine": "local",
        "fn": lambda p: local_tech.sync_status_all(top_n=_int(p, "top", 15)),
    },
    "verify": {
        "label": "Deep-verify top N",
        "engine": "local",
        "fn": lambda p: local_tech.verify_top_cli(top_n=_int(p, "top", 15),
                                                  max_workers=4),
    },
    "check-closed": {
        "label": "Probe stale URLs",
        "engine": "local",
        "fn": lambda p: local_tech.check_closed_jobs(
            stale_days=_int(p, "stale_days", 2), limit=_int(p, "limit")),
    },
    "rescore": {
        "label": "Rescore all",
        "engine": "local",
        "fn": lambda p: local_tech.rescore_all(
            described_only=bool(p.get("described_only", True))),
    },
    "backfill-descriptions": {
        "label": "Backfill descriptions",
        "engine": "local",
        "fn": lambda p: local_tech.backfill_board_descriptions(
            limit=_int(p, "limit")),
    },
    "backfill-workday": {
        "label": "Backfill Workday JDs",
        "engine": "local",
        "fn": lambda p: __import__(
            "jobcrawler.fetchers.workday",
            fromlist=["backfill_workday_descriptions"]
        ).backfill_workday_descriptions(limit=_int(p, "limit")),
    },
    "nlx": {
        "label": "NLx ingest",
        "engine": "local",
        "fn": lambda p: _op_nlx(p),
    },
    "discover-local": {
        "label": "Discover local companies",
        "engine": "local",
        "fn": lambda p: __import__(
            "jobcrawler.discovery.local_sourcing",
            fromlist=["populate_companies"]
        ).populate_companies(dork=not p.get("no_dork")),
    },
    "dork": {
        "label": "ATS dork sweep",
        "engine": "local",
        "fn": lambda p: __import__(
            "jobcrawler.discovery.ats_dork", fromlist=["run_ddgs_dorks"]
        ).run_ddgs_dorks(),
    },
    "score-missions": {
        "label": "Score missions",
        "engine": "local",
        "fn": lambda p: __import__(
            "jobcrawler.discovery.local_sourcing",
            fromlist=["score_missions"]
        ).score_missions(rescore_all=bool(p.get("rescore"))),
    },
    "add-board": {
        "label": "Add company board",
        "engine": "local",
        "fn": lambda p: __import__(
            "jobcrawler.discovery.local_sourcing", fromlist=["add_board"]
        ).add_board((p.get("name") or "").strip(), (p.get("url") or "").strip()),
    },
    "prune": {
        "label": "Prune dead boards",
        "engine": None,   # any track
        "fn": lambda p: _op_prune(p),
    },
    "dedup": {
        "label": "Dedup companies",
        "engine": None,   # any track
        "fn": lambda p: (lambda c: (store.dedup_companies(c), c.close()))(
            _conn(config.UI_TRACKS.get(p.get("track") or config.DEFAULT_TRACK))),
    },
    "add-job": {
        "label": "Add manual job",
        "engine": "local",
        "fn": lambda p: local_tech.add_manual_job(
            url=p.get("url", ""), title=p.get("title", ""),
            company=p.get("company", ""), location=p.get("location", "")),
    },
}


def _op_crawl(p):
    """ONE crawl command for every track: the difference between engines is
    configuration ([tracks.*].engine), not a separate button. The "local"
    engine runs the location-scoped store crawl; the "neural" engine runs the
    location-agnostic sweep (priority companies + aggregators + web search)
    against the track's own DB. Each engine swaps the shared keyword lists
    to its focus — _run_op restores the baseline before every op, so runs
    can't leak keywords into each other. Params not applicable to the active
    engine are simply ignored."""
    t = config.UI_TRACKS.get(p.get("track") or config.DEFAULT_TRACK)
    if t and t["engine"] == "neural":
        from jobcrawler.tracks import remote_neural_run
        argv = ["--commit"]
        if not p.get("no_fit"):
            argv.append("--fit")
        if p.get("no_websearch"):
            argv.append("--no-websearch")
        if p.get("confirm_cost"):
            argv.append("--confirm-cost")
        remote_neural_run.main(argv)
    else:
        local_tech.run(max_workers=_int(p, "workers", 6),
                       top_n=_int(p, "top", 15),
                       verify=not p.get("no_verify"))


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
    conn = _conn(config.UI_TRACKS.get(p.get("track") or config.DEFAULT_TRACK))
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
    if RESTARTING:
        return jsonify(error="server is restarting"), 409
    track_cfg = _track()
    need = OPS[name].get("engine")
    if need is not None and need != track_cfg["engine"]:
        return jsonify(error=f"{name!r} needs the {need!r} engine - the "
                             f"{track_cfg['label']} track runs "
                             f"{track_cfg['engine']!r}"), 409
    params = request.get_json(silent=True) or {}
    params["track"] = track_cfg["id"]
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


def _geo_tag(r):
    """Live geo bucket for a job row: "local" (configured locality),
    "remote", or "relocation" (onsite somewhere the user would have to move
    to). Derived at serve time from the location string — the stored
    geo_mode is stale-by-construction (computed against the locality config
    of whatever process ingested it) and overloaded (NULL/onsite ambiguity),
    so it's only consulted as a secondary remote signal."""
    loc = r.get("location") or ""
    if nc.NC_RE.search(loc):
        return "local"
    if (r.get("remote_eligible") or r.get("geo_mode") == "remote"
            or remote_filter.remote_signal(loc)):
        return "remote"
    return "relocation"


def _job_json(r, today, rank=None):
    d = {k: r.get(k) for k in _JOB_FIELDS}
    d["rank"] = rank
    d["age"] = local_tech._age_tag(r, today)
    d["verified"] = "deep:" in (r.get("fit_reason") or "")
    d["geo_bucket"] = _geo_tag(r)
    d["relocation_required"] = d["geo_bucket"] == "relocation"
    d["us_ok"] = remote_filter.us_eligible(r.get("location") or "")
    d["watched"] = "watch" in {t.strip() for t in
                               (r.get("company_tags") or "").split(",")}
    return d


@app.get("/api/jobs")
def api_jobs():
    t = _track()
    conn = _conn(t)
    # No server-side geo gate (location_re=None): every row in the track
    # comes back stamped with a live geo_bucket, and the CLIENT decides what
    # to show ("willing to relocate" checkbox, remote-requires-watch rule).
    # The digest/CLI callers of ranked_jobs keep their own geo gates — this
    # is a UI-only widening.
    rows = store.ranked_jobs(
        conn, track=t["track"], location_re=None,
        rank_by=t["rank_by"], min_mission=t["min_mission"],
        include_closed=request.args.get("closed") == "1",
        include_dispositioned=request.args.get("dispositioned") == "1")
    conn.close()
    today = _today()
    return jsonify([_job_json(r, today, i + 1) for i, r in enumerate(rows)])


@app.get("/api/tracks")
def api_tracks():
    return jsonify([
        {"id": t["id"], "label": t["label"], "engine": t["engine"],
         "min_fit_default": t["min_fit_default"],
         "willing_to_move_default": t["willing_to_move_default"],
         "remote_requires_watch": t["remote_requires_watch"],
         "default": t["id"] == config.DEFAULT_TRACK,
         "ops": sorted(n for n, o in OPS.items()
                       if o.get("engine") in (None, t["engine"]))}
        for t in config.UI_TRACKS.values()
    ])


@app.get("/api/job/<job_id>")
def api_job(job_id):
    conn = _conn(_track())
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
    conn = _conn(_track())
    row, err = store.set_disposition(conn, job_id, p.get("disposition", ""),
                                     note=(p.get("note") or "").strip() or None)
    conn.close()
    if err:
        return jsonify(error=err), 400
    return jsonify(ok=True, job_id=row["job_id"])


@app.get("/api/pipeline")
def api_pipeline():
    conn = _conn(_track())
    rows = store.get_pipeline(conn)
    conn.close()
    today = _today()
    return jsonify([_job_json(r, today) for r in rows])


@app.get("/api/companies")
def api_companies():
    conn = _conn(_track())
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
    conn = _conn(_track())
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
    conn = _conn(_track())
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
        conn = _conn(_track())
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
    conn = _conn(_track())
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM companies ORDER BY name").fetchall()]
    conn.close()
    for r in rows:
        r.pop("id", None)
    buf = io.BytesIO(json.dumps(rows, indent=1, ensure_ascii=False).encode("utf-8"))
    return send_file(buf, mimetype="application/json", as_attachment=True,
                     download_name="company_roster.json")


# --------------------------------------------------------------------------- #
#  Config editing (Settings tab) + graceful self-restart                        #
# --------------------------------------------------------------------------- #

def _schedule_restart():
    """Spawn a detached successor process on the same port and exit. The
    successor runs --takeover (waits for this process's socket to free up
    instead of bailing out on the already-running probe). Works for both
    `python webapp.py` and the Nuitka exe (sys.argv[0] is the exe)."""
    global RESTARTING
    RESTARTING = True

    def worker():
        import time
        time.sleep(0.75)          # let the HTTP response flush to the browser
        if "__compiled__" in globals():
            cmd = [sys.argv[0]]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve())]
        cmd += [f"--port={BOUND_PORT}", "--no-open", "--takeover"]
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(cmd, cwd=str(config.SCRIPT_DIR),
                         close_fds=True, creationflags=flags)
        os._exit(0)

    threading.Thread(target=worker, daemon=True).start()


def _config_busy():
    if _running():
        return jsonify(error=f"'{TASK['name']}' is running - "
                             "wait for it to finish before saving config"), 409
    if RESTARTING:
        return jsonify(error="server is restarting"), 409
    return None


@app.get("/api/config")
def api_config_get():
    raw, source = profile_edit.read_raw()
    try:
        import tomllib
        parsed = tomllib.loads(raw)
    except Exception:
        parsed = {}
    return jsonify(raw=raw, source=source, parsed=parsed)


@app.post("/api/config/validate")
def api_config_validate():
    p = request.get_json(silent=True) or {}
    return jsonify(errors=profile_edit.validate(p.get("toml") or ""))


@app.put("/api/config")
def api_config_put():
    busy = _config_busy()
    if busy:
        return busy
    p = request.get_json(silent=True) or {}
    updates = p.get("updates") or {}
    if not isinstance(updates, dict) or not updates:
        return jsonify(error="no updates given"), 400
    try:
        text = profile_edit.apply_updates(updates)
    except Exception as e:
        return jsonify(error=f"could not apply updates: {e}"), 400
    errors = profile_edit.validate(text)
    if errors:
        return jsonify(error="validation failed", errors=errors), 400
    profile_edit.backup_then_write(text)
    _schedule_restart()
    return jsonify(ok=True, restarting=True)


@app.put("/api/config/raw")
def api_config_put_raw():
    busy = _config_busy()
    if busy:
        return busy
    p = request.get_json(silent=True) or {}
    text = p.get("toml") or ""
    errors = profile_edit.validate(text)
    if errors:
        return jsonify(error="validation failed", errors=errors), 400
    profile_edit.backup_then_write(text)
    _schedule_restart()
    return jsonify(ok=True, restarting=True)


@app.get("/api/stats")
def api_stats():
    t = _track()
    conn = _conn(t)

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
        "db": str(t["db_path"]),
        "track": t["id"],
        "boot_id": BOOT_ID,
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

    global BOUND_PORT
    BOUND_PORT = port
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
