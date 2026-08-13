"""Background operation registry + runner (one op at a time, console tee'd
to the browser via /api/run/status polling)."""

import io
import sys
import threading
from datetime import datetime

import config
from core import store
from scrapers import ops as maint

TASK = {"name": None, "thread": None, "log": [], "started": None,
        "ended": None, "error": None}
_LOG_LOCK = threading.Lock()


class _Tee(io.TextIOBase):
    """stdout tee: real console keeps printing; the browser polls the copy.
    Swapped in globally while an operation runs so the crawl's many
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


# Pristine keyword lists, captured at import — BEFORE any op runs. Every
# track runs in THIS process: extend-mode tracks grow the shared lists,
# replace-mode tracks swap them — without a reset between ops, running one
# track's crawl would poison the next one's keyword filter. Restored in
# place (slice assignment) so modules that bound the list objects at import
# time (core/filters.py) see the reset.
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


def _op_track(p):
    """The track cfg an op should run against (api_run injects p["track"])."""
    return config.UI_TRACKS.get(p.get("track") or config.DEFAULT_TRACK)


def _op_crawl(p):
    """ONE crawl command for every track: scrapers/runner.run_track, the
    single pipeline whose methodology (keyword handling, source families,
    gates, scoring budget, digest/email) comes entirely from the track's
    profile.toml [tracks.*] config. The track's keyword focus swaps the
    shared keyword lists — _run_op restores the baseline before every op,
    so runs can't leak keywords into each other."""
    from scrapers import runner
    runner.run_track(
        _op_track(p), commit=True,
        fit=not p.get("no_fit"),
        verify=False if p.get("no_verify") else None,   # None = track config
        websearch=False if p.get("no_websearch") else None,
        confirm_cost=bool(p.get("confirm_cost")),
        max_workers=_int(p, "workers", 6),
        top_n=_int(p, "top", 15))


def _op_nlx(p):
    """Pull postings for bot-gated employers (Meta/Google/Qualcomm...) from
    the federal NLx feed and run them through the standard ingest."""
    from scrapers.fetchers.careeronestop import fetch_nlx_company
    names = [n.strip() for n in (p.get("companies") or "").split(",") if n.strip()]
    if not names:
        print("  [!] give a comma-separated list of employer names")
        return
    total = 0
    for name in names:
        jobs = fetch_nlx_company(name)
        print(f"  {name}: {len(jobs)} NLx posting(s)")
        if jobs:
            total += maint.ingest_external_jobs(jobs, source="nlx",
                                                t=_op_track(p))
    print(f"  {total} new job(s) ingested from the NLx feed.")


def _op_prune(p):
    conn = store.connect(_op_track(p)["db_path"])
    try:
        store.prune_dead_boards(
            conn, deactivate_offmission=bool(p.get("offmission")))
    finally:
        conn.close()


def _op_dedup(p):
    conn = store.connect(_op_track(p)["db_path"])
    try:
        store.dedup_companies(conn)
    finally:
        conn.close()


# Each op declares which crawl ENGINE it needs ("local" = the location-scoped
# crawler, "neural" = the location-agnostic sweep — scrapers/runner.py;
# None = engine-agnostic, runs against whichever track is active). Ops are
# matched to the active track by its profile-configured `engine` — never by
# the user-chosen track id.
OPS = {
    "crawl": {
        "label": "Crawl",
        "engine": None,   # one command for every track — dispatches on engine
        "fn": lambda p: _op_crawl(p),
    },
    "sync": {
        "label": "Sync statuses",
        "engine": None,
        "fn": lambda p: maint.sync_status_all(top_n=_int(p, "top", 15),
                                              t=_op_track(p)),
    },
    "verify": {
        "label": "Deep-verify top N",
        "engine": None,
        "fn": lambda p: maint.verify_top_cli(top_n=_int(p, "top", 15),
                                             max_workers=4, t=_op_track(p)),
    },
    "check-closed": {
        "label": "Probe stale URLs",
        "engine": None,
        "fn": lambda p: maint.check_closed_jobs(
            stale_days=_int(p, "stale_days", 2), limit=_int(p, "limit"),
            t=_op_track(p)),
    },
    "rescore": {
        "label": "Rescore all",
        "engine": None,
        "fn": lambda p: maint.rescore_all(
            described_only=bool(p.get("described_only", True)),
            t=_op_track(p)),
    },
    "backfill-descriptions": {
        "label": "Backfill descriptions",
        "engine": None,
        "fn": lambda p: maint.backfill_board_descriptions(
            limit=_int(p, "limit"), t=_op_track(p)),
    },
    "backfill-workday": {
        "label": "Backfill Workday JDs",
        "engine": "local",
        "fn": lambda p: __import__(
            "scrapers.fetchers.workday",
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
            "discovery.local_sourcing",
            fromlist=["populate_companies"]
        ).populate_companies(dork=not p.get("no_dork")),
    },
    "dork": {
        "label": "ATS dork sweep",
        "engine": "local",
        "fn": lambda p: __import__(
            "discovery.ats_dork", fromlist=["run_ddgs_dorks"]
        ).run_ddgs_dorks(),
    },
    "score-missions": {
        "label": "Score missions",
        "engine": "local",
        "fn": lambda p: __import__(
            "discovery.local_sourcing",
            fromlist=["score_missions"]
        ).score_missions(rescore_all=bool(p.get("rescore"))),
    },
    "add-board": {
        "label": "Add company board",
        "engine": "local",
        "fn": lambda p: __import__(
            "discovery.local_sourcing", fromlist=["add_board"]
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
        "fn": lambda p: _op_dedup(p),
    },
    "add-job": {
        "label": "Add manual job",
        "engine": None,
        "fn": lambda p: maint.add_manual_job(
            url=p.get("url", ""), title=p.get("title", ""),
            company=p.get("company", ""), location=p.get("location", ""),
            t=_op_track(p)),
    },
}
