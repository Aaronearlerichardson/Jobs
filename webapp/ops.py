"""Background operation registry + runner (one op at a time, console tee'd
to the browser via /api/run/status polling)."""

import io
import sys
import threading
from datetime import datetime

import config
from core import session_log, store
from scrapers import ops as maint

TASK = {"name": None, "thread": None, "log": [], "log_offset": 0,
        "started": None, "ended": None, "error": None, "active": False}
_LOG_LOCK = threading.Lock()
# Guards the claim on TASK. Separate from _LOG_LOCK, which the Tee takes on
# every write — holding one while waiting on the other would deadlock.
_TASK_LOCK = threading.Lock()


class _Tee(io.TextIOBase):
    """stdout tee: real console keeps printing; the browser polls the copy,
    and an optional `sink` SessionLog (see core/session_log.py) gets a
    third copy as it streams — mirrored there as timestamped, levelled
    records — so UI-triggered runs are reviewable after the fact just like
    CLI ones. Swapped in globally while an operation runs so the crawl's
    many worker-thread print()s are captured too.

    The browser tracks a cursor into the log (`since=<n>` on
    /api/run/status) so a poll only re-sends lines it hasn't seen yet. That
    cursor has to be an ABSOLUTE line count — total lines ever appended —
    not a raw index into TASK["log"], because the list below gets its head
    chopped off once it grows past 5000 lines. A raw-index cursor goes stale
    the instant a trim shifts every surviving line down by 1000: the same
    index now names an earlier line, and the client re-renders lines it
    already showed (the browser-side duplication bug this class exists to
    avoid). log_offset counts lines permanently dropped by trimming, so
    routes.py can translate an absolute cursor back into a list index
    (`since - log_offset`) that stays correct across trims."""

    def __init__(self, orig, sink=None):
        self.orig = orig
        self.sink = sink
        # Partial lines keyed by writing thread: print() issues separate
        # text/newline writes, and one shared buffer let a fetch worker's
        # line fuse into the middle of a progress line in the browser log
        # (and the session log — its SessionLog sink assembles per-thread
        # the same way).
        self._bufs = {}

    def write(self, s):
        try:
            self.orig.write(s)
        except Exception:
            pass
        if self.sink is not None:
            try:
                self.sink.write(s)
            except Exception:
                pass
        key = threading.get_ident()
        with _LOG_LOCK:
            buf = self._bufs.get(key, "") + s
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                TASK["log"].append(line)
                if len(TASK["log"]) > 5000:
                    del TASK["log"][:1000]
                    TASK["log_offset"] += 1000
            if buf:
                self._bufs[key] = buf
            else:
                self._bufs.pop(key, None)
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
    """Start `fn` on a worker thread. Returns False if an operation is
    already running.

    The claim is taken under a lock rather than left to the caller's
    `_running()` check. Two /api/run requests arriving together could both
    pass that check before either set TASK["thread"], and the second op then
    ran concurrently with the first — doing the whole crawl twice and, worse,
    nesting the stdout tee: each layer appends to TASK["log"], so every line
    landed in the browser log once per layer.
    """
    def worker():
        orig = sys.stdout
        try:
            slog = session_log.open_log(f"webui-{name}",
                                        f"web UI op {name!r}")
        except OSError:
            slog = None              # a full/read-only disk can't block the op
        tee = _Tee(orig, sink=slog)
        sys.stdout = tee
        try:
            _restore_keywords()
            fn()
        except Exception as e:
            TASK["error"] = f"{type(e).__name__}: {e}"
            print(f"  [!] operation failed: {TASK['error']}")
        finally:
            if slog is not None:
                slog.close()
            # Only unwind our own layer. Blindly assigning `orig` back would
            # restore a stale stream if anything else swapped stdout while we
            # ran, permanently leaving a tee installed that copies every later
            # print into the op log.
            if sys.stdout is tee:
                sys.stdout = orig
            TASK["ended"] = datetime.now().isoformat()
            with _TASK_LOCK:
                TASK["active"] = False

    with _TASK_LOCK:
        if _running():
            return False
        TASK["active"] = True          # claim before the thread exists —
        TASK["thread"] = None          # an unstarted thread isn't yet alive
        TASK.update(name=name, error=None, ended=None,
                    started=datetime.now().isoformat())
    with _LOG_LOCK:
        TASK["log"].clear()
        TASK["log_offset"] = 0
    t = threading.Thread(target=worker, daemon=True)
    TASK["thread"] = t
    try:
        t.start()
    except Exception:
        with _TASK_LOCK:
            TASK["active"] = False
        raise
    return True


def _running():
    if TASK["active"]:
        return True
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


# The bulk-discovery ops (discover-local, dork, discover-term) are CLI-only
# now: `python discover.py --local / --dork / "<term>"`. A 29-minute
# discover-local run found 4 new boards, an ATS-dork sweep names companies
# after a de-hyphenated slug, and 84 pasted companies produced one good job
# ever — none of that is worth a button that ties up the single op slot for
# half an hour. What the UI keeps is the targeted paths (add-names,
# add-board, add-job) plus the Review queue every one of them feeds.


def _op_add_names(p):
    """Resolve the names a person ticked in the Review step.

    `params["names"]` is the confirmed LIST from /api/names/preview; a raw
    string is still accepted (add_names parses it) so an older client, or a
    scripted POST, keeps working."""
    from discovery.local_sourcing import add_names
    add_names(p.get("names") or [], use_llm=bool(p.get("use_llm")))


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
        store.dedup_jobs(conn)
    finally:
        conn.close()


# Each op declares which crawl ENGINE it needs ("local" = the location-scoped
# crawler, "sweep" = the location-agnostic one — scrapers/runner.py;
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
                                             max_workers=4, t=_op_track(p),
                                             force=bool(p.get("force"))),
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
    "add-names": {
        "label": "Add companies from pasted text",
        "engine": "local",
        "fn": lambda p: _op_add_names(p),
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
    "reresolve": {
        "label": "Retry unresolved companies",
        "engine": None,   # any track
        "fn": lambda p: maint.reresolve_misses(
            limit=_int(p, "limit", 50), days=_int(p, "days"),
            t=_op_track(p)),
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
