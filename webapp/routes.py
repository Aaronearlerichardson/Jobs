"""All HTTP routes: jobs/pipeline/companies/stats/tracks, the background-op
API, config editing (Settings tab), and the SPA itself."""

import hashlib
import io
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from flask import jsonify, make_response, request, send_file

import config
from core import digest_md, locality, profile_edit, remote_filter, store
from core import tags as company_tags

from . import BOOT_ID, STATE, app
from .ops import _LOG_LOCK, OPS, TASK, _int, _run_op, _running
from .server import schedule_restart


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
#  Background operations                                                        #
# --------------------------------------------------------------------------- #

@app.post("/api/run/<name>")
def api_run(name):
    if name not in OPS:
        return jsonify(error=f"unknown operation {name!r}"), 404
    if _running():
        return jsonify(error=f"'{TASK['name']}' is already running"), 409
    if STATE["restarting"]:
        return jsonify(error="server is restarting"), 409
    track_cfg = _track()
    need = OPS[name].get("engine")
    if need is not None and need != track_cfg["engine"]:
        return jsonify(error=f"{name!r} needs the {need!r} engine - the "
                             f"{track_cfg['label']} track runs "
                             f"{track_cfg['engine']!r}"), 409
    params = request.get_json(silent=True) or {}
    params["track"] = track_cfg["id"]
    # The _running() check above is advisory — it gives a nicer message naming
    # the running op. This is the authoritative one: it claims the slot under a
    # lock, so two requests in the same instant can't both start an operation.
    if not _run_op(name, lambda: OPS[name]["fn"](params)):
        return jsonify(error=f"'{TASK['name']}' is already running"), 409
    return jsonify(ok=True, name=name)


@app.get("/api/run/status")
def api_run_status():
    # `since` is an ABSOLUTE line count (see _Tee's docstring in ops.py), not
    # a raw index into TASK["log"] — that list gets its head chopped off once
    # it passes 5000 lines, so a plain-index cursor goes stale on every trim
    # and the browser re-renders lines it already showed. log_offset is how
    # many lines have been trimmed away; subtracting it from the absolute
    # cursor gives the correct index into what remains.
    since = _int(request.args, "since", 0) or 0
    with _LOG_LOCK:
        offset = TASK["log_offset"]
        start = max(0, since - offset)
        lines = TASK["log"][start:]
        total = offset + len(TASK["log"])
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
    # Application-pipeline tracking (store.PIPELINE_FIELDS + the stamp).
    "applied_at", "followup_at", "contact", "referral", "outcome_reason",
)


def _geo_tag(r):
    """Live geo bucket for a job row: "local" (configured locality),
    "remote", or "relocation" (onsite somewhere the user would have to move
    to). Derived at serve time from the location string — the stored
    geo_mode is stale-by-construction (computed against the locality config
    of whatever process ingested it) and overloaded (NULL/onsite ambiguity),
    so it's only consulted as a secondary remote signal."""
    loc = r.get("location") or ""
    if locality.NC_RE.search(loc):
        return "local"
    if (r.get("remote_eligible") or r.get("geo_mode") == "remote"
            or remote_filter.remote_signal(loc)):
        return "remote"
    return "relocation"


def _job_json(r, today, rank=None, remote_floor=None):
    d = {k: r.get(k) for k in _JOB_FIELDS}
    d["rank"] = rank
    d["age"] = digest_md.age_tag(r, today)
    d["verified"] = "deep:" in (r.get("fit_reason") or "")
    d["geo_bucket"] = _geo_tag(r)
    d["relocation_required"] = d["geo_bucket"] == "relocation"
    d["us_ok"] = remote_filter.us_eligible(r.get("location") or "")
    d["watched"] = "watch" in {t.strip() for t in
                               (r.get("company_tags") or "").split(",")}
    # Whether a REMOTE row here is worth showing in a location-scoped track.
    # The same rule ranked_jobs applies server-side, re-run per row because
    # /api/jobs deliberately ships everything and gates on the client.
    d["remote_ok"] = store.remote_admitted(r, remote_floor)
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
    floor = t.get("remote_mission_floor")
    return jsonify([_job_json(r, today, i + 1, remote_floor=floor)
                    for i, r in enumerate(rows)])


@app.get("/api/tracks")
def api_tracks():
    return jsonify([
        {"id": t["id"], "label": t["label"], "engine": t["engine"],
         "min_fit_default": t["min_fit_default"],
         "willing_to_move_default": t["willing_to_move_default"],
         "remote_requires_watch": t["remote_requires_watch"],
         "remote_mission_floor": t["remote_mission_floor"],
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
    d["age"] = digest_md.age_tag(d, _today())
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


@app.post("/api/job/<job_id>/pipeline")
def api_pipeline_fields(job_id):
    """Edit one application's tracking fields (store.PIPELINE_FIELDS).

    Only the keys actually present in the body are written, so the SPA's
    save-on-change editor can send one field at a time without blanking the
    others. The whitelist lives in the store, not here.
    """
    p = request.get_json(silent=True) or {}
    fields = {k: p[k] for k in store.PIPELINE_FIELDS if k in p}
    t = _track()
    conn = _conn(t)
    row, err = store.update_pipeline_fields(conn, job_id, **fields)
    conn.close()
    if err:
        return jsonify(error=err), 400
    return jsonify(ok=True, job=_job_json(
        row, _today(), remote_floor=t.get("remote_mission_floor")))


@app.get("/api/pipeline")
def api_pipeline():
    t = _track()
    conn = _conn(t)
    today = _today()
    floor = t.get("remote_mission_floor")
    rows = [_job_json(r, today, remote_floor=floor)
            for r in store.get_pipeline(conn)]
    due = [j["job_id"] for j in store.followups_due(conn, today)]
    conn.close()
    return jsonify(rows=rows, followups_due=due)


@app.get("/api/report/conversion")
def api_conversion():
    """Applications per fit band x geo_mode, with the interview rate — the
    Pipeline tab's answer to "which kind of job is actually converting?"."""
    conn = _conn(_track())
    report = store.conversion_report(conn)
    conn.close()
    return jsonify(report)


@app.get("/api/companies")
def api_companies():
    conn = _conn(_track())
    comps = store.get_companies(conn, active_only=False)
    # Open-job count AND best résumé fit per company in one pass: the roster
    # suggests watching a company that has already produced a good-fit job,
    # which is how a hand-maintained watch list is meant to grow.
    stats = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT company_id, COUNT(*), MAX(resume_fit_score) FROM jobs "
        "WHERE COALESCE(status,'open')!='closed' AND company_id IS NOT NULL "
        "GROUP BY company_id").fetchall()}
    conn.close()
    out = []
    for c in comps:
        tags = {t for t in (c.get("tags") or "").split(",") if t}
        n_open, best_fit = stats.get(c["id"], (0, None))
        out.append({
            "id": c["id"], "name": c["name"], "ats": c.get("ats"),
            "mission_tier": c.get("mission_tier"),
            "mission_score": c.get("mission_score"),
            "active": bool(c.get("active")), "tags": sorted(tags),
            "watched": "watch" in tags, "open_jobs": n_open,
            "best_fit": best_fit,
            # Crawl cadence, so the roster shows WHY a company stopped
            # producing rows instead of looking silently broken.
            "crawl_state": c.get("crawl_state") or "active",
            "empty_streak": c.get("empty_streak") or 0,
            "next_crawl_at": c.get("next_crawl_at"),
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


@app.post("/api/company/<int:cid>/reactivate")
def api_reactivate(cid):
    """Undormant a company: crawl it every run again. The manual override
    for a board the dormancy rules retired too eagerly (a slug that was
    briefly broken, a team that has only just started hiring)."""
    conn = _conn(_track())
    row = conn.execute("SELECT id FROM companies WHERE id=?", (cid,)).fetchone()
    if not row:
        conn.close()
        return jsonify(error="not found"), 404
    store.reactivate_company(conn, cid)
    conn.close()
    return jsonify(ok=True, crawl_state="active")


@app.post("/api/company/<int:cid>/active")
def api_active(cid):
    on = 1 if (request.get_json(silent=True) or {}).get("on") else 0
    conn = _conn(_track())
    conn.execute("UPDATE companies SET active=? WHERE id=?", (on, cid))
    conn.commit()
    conn.close()
    return jsonify(ok=True, active=bool(on))


# --------------------------------------------------------------------------- #
#  Roster review queue                                                         #
# --------------------------------------------------------------------------- #
#
# Nothing an automated path discovers joins the roster by itself: it lands as
# an inactive, pending-review company row (core/store.py) and waits here.


@app.get("/api/pending")
def api_pending():
    conn = _conn(_track())
    rows = store.pending_companies(conn)
    conn.close()
    return jsonify(rows)


@app.post("/api/company/<int:cid>/confirm")
def api_confirm(cid):
    """Accept a review candidate: the pending tag comes off and the shared
    mission rule decides whether it is crawled."""
    conn = _conn(_track())
    row = store.confirm_company(conn, cid)
    conn.close()
    if not row:
        return jsonify(error="not found"), 404
    return jsonify(ok=True, name=row["name"], active=bool(row["active"]))


@app.post("/api/company/<int:cid>/reject")
def api_reject(cid):
    """Throw a review candidate away: the row and its jobs go, and the name
    is blocklisted so discovery stops re-finding it."""
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip()
    conn = _conn(_track())
    name = store.reject_company(conn, cid, reason or "rejected in review")
    conn.close()
    if not name:
        return jsonify(error="not found"), 404
    return jsonify(ok=True, name=name)


@app.post("/api/names/preview")
def api_names_preview():
    """Parse a pasted page into review rows WITHOUT resolving anything.

    Step one of the paste flow: resolving is what costs the requests, so the
    names are shown first and only the ticked ones are sent to the op."""
    if _running():
        return jsonify(error=f"'{TASK['name']}' is already running"), 409
    from discovery.local_sourcing import preview_names
    p = request.get_json(silent=True) or {}
    raw = p.get("use_llm")
    try:
        names = preview_names(p.get("text") or "",
                              use_llm=None if raw is None else bool(raw))
    except Exception as e:
        return jsonify(error=f"could not parse that text: {e}"), 400
    return jsonify(names)


@app.post("/api/names/block")
def api_names_block():
    """Blocklist the names a person marked 'not a company', so no discovery
    path spends requests on them again."""
    if _running():
        return jsonify(error=f"'{TASK['name']}' is already running"), 409
    p = request.get_json(silent=True) or {}
    reason = (p.get("reason") or "").strip() or "not a company (review)"
    conn = _conn(_track())
    keys = {k for k in (store.block_name(conn, n, reason)
                        for n in (p.get("names") or [])) if k}
    conn.close()
    return jsonify(ok=True, blocked=len(keys), keys=sorted(keys))


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

def _config_busy():
    if _running():
        return jsonify(error=f"'{TASK['name']}' is running - "
                             "wait for it to finish before saving config"), 409
    if STATE["restarting"]:
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
    schedule_restart()
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
    schedule_restart()
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
        # Applications that went out in the last 7 days: the volume the
        # apply band exists to raise. applied_at is stamped once, on the
        # first 'applied', so a row since moved to interviewing/rejected
        # still counts toward the week it went out in.
        "applied_7d": one("SELECT COUNT(*) FROM jobs "
                          "WHERE substr(applied_at,1,10) >= ?",
                          ((datetime.now() - timedelta(days=7))
                           .strftime("%Y-%m-%d"),)),
        # Companies actually crawled every run: a dormant row is still
        # active, but only comes round weekly, so counting it here
        # overstated the roster by roughly 60%.
        "companies_active": one("SELECT COUNT(*) FROM companies WHERE "
                                "active=1 AND "
                                "COALESCE(crawl_state,'active')='active'"),
        # Roster GROWTH, from companies.created_at. last_probed cannot answer
        # this: a bulk mission re-score rewrites it on every row.
        "companies_new_7d": store.roster_growth(conn, days=7),
        # Candidates that failed to become crawlable companies, per reason
        # family — the worklist behind a roster that stopped growing.
        "company_misses": dict(store.miss_counts(conn)),
        "watched": one("SELECT COUNT(*) FROM companies WHERE "
                       "(','||COALESCE(tags,'')||',') LIKE '%,watch,%'"),
        # Roster candidates waiting on a human — the Review tab's badge.
        "pending_review": one("SELECT COUNT(*) FROM companies WHERE "
                              "(','||COALESCE(tags,'')||',') LIKE ?",
                              (f"%,{company_tags.PENDING},%",)),
        "api_key": config.ANTHROPIC_API_KEY != "YOUR_ANTHROPIC_API_KEY_HERE",
        "screen_model": config.CLAUDE_MODEL,
        "verify_model": config.CLAUDE_VERIFY_MODEL,
        "db": str(t["db_path"]),
        # Where the Settings tab writes. Shown in the header because the two
        # can diverge (a per-user data dir vs. a profile beside the code).
        "profile": str(config.PROFILE_PATH),
        "track": t["id"],
        "boot_id": BOOT_ID,
    }
    conn.close()
    return jsonify(stats)


def _asset_version():
    """Fingerprint of the front-end files, for cache-busting."""
    h = hashlib.md5()
    for p in sorted((Path(app.root_path) / "static").rglob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()[:10]


@app.get("/")
def index():
    # Served as a plain file, NOT via Jinja — the SPA's JS contains
    # template-looking fragments a render pass would corrupt. The only
    # rewriting is a cache-buster stamped onto the asset URLs: the CSS/JS
    # live at fixed paths, so a browser that cached an older copy would
    # otherwise keep serving it across restarts (a half-written app.js
    # renders the shell with no tiles, no jobs, and no error you'd notice).
    html = (Path(app.root_path) / "templates" / "index.html").read_text(
        encoding="utf-8")
    html = re.sub(r'(?<=["\'])(/static/[^"\']+?)(?:\?v=[^"\']*)?(?=["\'])',
                  lambda m: f"{m.group(1)}?v={_asset_version()}", html)
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.after_request
def _no_store_assets(resp):
    """Never let the browser reuse a stale front-end. Single-user localhost:
    correctness beats the microscopic win from caching a 40 KB file."""
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp
