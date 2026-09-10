"""ONE registry of the crawler's operations, shared by every front end.

The web UI (webapp/ops.py), the maintenance CLI (run_scraper.py) and the
roster CLI (discover.py) used to each carry their own dispatch table over
the same functions, and they drifted: a helper was lost in a refactor
while the button that called it survived, and the CLI and the UI passed
different defaults to the same call. Now each operation is declared once:

    name -> {label, engine, target, params[, ui]}

  label    what the UI shows on the button
  engine   the crawl engine the op needs ("local" / "sweep"), or None for
           any track. Matched against the active track's profile-
           configured `engine`, never against a user-chosen track id.
  target   "module:function", resolved lazily with importlib on each
           call — importing this module pulls in nothing heavy, and a
           test's monkeypatch on the target module is honored.
  params   how a front end's flat {key: value} dict becomes the target's
           keyword arguments (see `Param`).
  ui       False hides the op from the web UI (CLI-only shape).

A front end hands `invoke()` the op name and a params dict — the JSON the
browser POSTed, or values pulled off argparse — and gets the target's
return value back. Parameter names are the same on both sides, so a
button and a flag are two spellings of one call.
"""

import importlib
from typing import NamedTuple

import config

#: "No default": when the key is absent the kwarg is not passed at all, so
#: the target's own default applies.
OMIT = object()

#: "Not given": `invoke(track=UNSET)` resolves the track from params.
UNSET = object()


class Param(NamedTuple):
    """One target keyword argument and where it comes from.

    key      the name in the params dict (what the browser posts / the CLI
             passes); for "track" / "db_path" kinds it is informational
    kw       the target's keyword argument (defaults to `key`)
    kind     how the raw value is coerced — see `coerce`
    default  the RAW value assumed when the key is absent or blank; OMIT
             (the default) leaves the kwarg out instead
    """
    key: str
    kw: str = None
    kind: str = "str"
    default: object = OMIT


def _names(v):
    """A comma-separated string, or a list, as a clean list of names."""
    if isinstance(v, str):
        v = v.split(",")
    return [str(s).strip() for s in (v or []) if str(s).strip()]


def coerce(kind, v):
    """A raw front-end value as the target expects it.

    >>> coerce("int", " 5 "), coerce("bool", "yes"), coerce("str", " x ")
    (5, True, 'x')
    >>> coerce("int", None) is None       # a None default passes through
    True
    >>> coerce("not", True), coerce("not", None)      # no_fit -> fit
    (False, True)
    >>> coerce("veto", True), coerce("veto", None)    # no_verify -> verify
    (False, None)
    >>> coerce("assert", True), coerce("assert", "")  # --send -> send
    (True, None)
    >>> coerce("names", "Acme, , Globex"), coerce("names", ["A", " "])
    (['Acme', 'Globex'], ['A'])
    >>> coerce("raw", {"k": 1})
    {'k': 1}

    Notes:
        "veto" is how a "skip X" checkbox or flag maps onto a target whose
        `X=None` means "use the track's own setting": ticked -> False,
        otherwise None. "assert" is the mirror image for "force X on".
    """
    if kind == "int":
        return None if v is None else int(str(v).strip())
    if kind == "bool":
        return bool(v)
    if kind == "str":
        return str(v or "").strip()
    if kind == "not":
        return not v
    if kind == "veto":
        return False if v else None
    if kind == "assert":
        return True if v else None
    if kind == "names":
        return _names(v)
    if kind == "raw":
        return v
    raise ValueError(f"unknown param kind {kind!r}")


def build_kwargs(spec, params, track):
    """The target's kwargs from a params dict, per `spec` (a Param list).

    Absent keys (missing, None, or blank) take the Param's default; with
    no default the kwarg is left out. "track" and "db_path" kinds ignore
    the params dict and pass the resolved track (or its store path).

    >>> spec = [Param("top", "top_n", "int", 15), Param("limit", kind="int"),
    ...         Param("no_verify", "verify", "veto"),
    ...         Param("track", "t", "track"), Param("track", "db_path", "db_path")]
    >>> t = {"id": "x", "db_path": "x.db"}
    >>> build_kwargs(spec, {"top": "3", "limit": ""}, t)
    {'top_n': 3, 'verify': None, 't': {'id': 'x', 'db_path': 'x.db'}, 'db_path': 'x.db'}
    >>> build_kwargs(spec, {"limit": 7, "no_verify": True}, None)
    {'top_n': 15, 'limit': 7, 'verify': False, 't': None, 'db_path': None}
    """
    out = {}
    for p in spec:
        kw = p.kw or p.key
        if p.kind == "track":
            out[kw] = track
            continue
        if p.kind == "db_path":
            out[kw] = track["db_path"] if track else None
            continue
        v = params.get(p.key)
        present = v is not None and v != ""
        if p.kind in ("veto", "assert", "not") and not present:
            v = None if p.default is OMIT else p.default
        elif not present:
            if p.default is OMIT:
                continue
            v = p.default
        out[kw] = coerce(p.kind, v)
    return out


def resolve(target):
    """The callable a "module:function" target names, imported on demand.

    >>> import os.path
    >>> resolve("os.path:join") is os.path.join
    True
    """
    mod, _, attr = target.partition(":")
    return getattr(importlib.import_module(mod), attr)


# Shared param rows. `_TRACK` hands the target the track cfg; the CLI passes
# whatever --track resolved to (None = the target's default-track rule).
_TRACK = Param("track", "t", "track")
_DB_PATH = Param("track", "db_path", "db_path")
_LIMIT = Param("limit", kind="int", default=None)
_WORKERS = Param("workers", "max_workers", "int")        # omitted if absent

REGISTRY = {
    # ── the crawl ─────────────────────────────────────────────────────
    "crawl": {
        "label": "Crawl",
        "engine": None,   # one command for every track — dispatches on engine
        "target": "scrapers.runner:run_track",
        "params": [
            _TRACK,
            Param("no_fit", "fit", "not", False),
            Param("preview", "commit", "not", False),
            Param("send", "send", "assert"),
            Param("no_verify", "verify", "veto"),
            Param("no_websearch", "websearch", "veto"),
            Param("confirm_cost", kind="bool", default=False),
            Param("workers", "max_workers", "int", 6),
            Param("top", "top_n", "int", 15),
            Param("samples", kind="int"),
        ],
    },
    # ── maintenance (any track) ───────────────────────────────────────
    "sync": {
        "label": "Sync statuses",
        "engine": None,
        "target": "scrapers.ops:sync_status_all",
        "params": [Param("top", "top_n", "int", 15), _TRACK],
    },
    "verify": {
        "label": "Deep-verify top N",
        "engine": None,
        "target": "scrapers.ops:verify_top_cli",
        "params": [Param("top", "top_n", "int", 15),
                   Param("workers", "max_workers", "int", 4),
                   Param("force", kind="bool", default=False), _TRACK],
    },
    "check-closed": {
        "label": "Probe stale URLs",
        "engine": None,
        "target": "scrapers.ops:check_closed_jobs",
        "params": [Param("stale_days", kind="int", default=2), _LIMIT,
                   _WORKERS, _TRACK],
    },
    "triage": {
        "label": "Triage harvested rows",
        "engine": None,
        "target": "scrapers.triage:run",
        "params": [_DB_PATH, _LIMIT, _WORKERS,
                   Param("score_cap", kind="int")],
    },
    "rescore": {
        "label": "Rescore all",
        "engine": None,
        "target": "scrapers.ops:rescore_all",
        "params": [Param("described_only", kind="bool", default=True),
                   _WORKERS, _TRACK],
    },
    "backfill-descriptions": {
        "label": "Backfill descriptions",
        "engine": None,
        "target": "scrapers.ops:backfill_board_descriptions",
        "params": [_LIMIT, _WORKERS, _TRACK],
    },
    "backfill-workday": {
        "label": "Backfill Workday JDs",
        "engine": "local",
        "target": "scrapers.fetchers.workday:backfill_workday_descriptions",
        "params": [_LIMIT, _WORKERS],
    },
    "backfill-axes": {
        "label": "Backfill fit axes",
        "engine": None,
        "target": "core.ops_targets:backfill_axes",
        "params": [_TRACK],
        "ui": False,      # offline column fill; a CLI repair, not a button
    },
    "reresolve": {
        "label": "Retry unresolved companies",
        "engine": None,
        "target": "scrapers.ops:reresolve_misses",
        "params": [Param("limit", kind="int", default=50),
                   Param("days", kind="int", default=None), _WORKERS, _TRACK],
    },
    "prune": {
        "label": "Prune dead boards",
        "engine": None,
        "target": "core.ops_targets:prune",
        "params": [Param("offmission", kind="bool", default=False), _TRACK],
    },
    "dedup": {
        "label": "Dedup companies",
        "engine": None,
        "target": "core.ops_targets:dedup",
        "params": [_TRACK],
    },
    "add-job": {
        "label": "Add manual job",
        "engine": None,
        "target": "scrapers.ops:add_manual_job",
        "params": [Param("url", default=""), Param("title", default=""),
                   Param("company", default=""), Param("location", default=""),
                   _TRACK],
    },
    # ── roster growth (the location-scoped engine's store) ────────────
    "nlx": {
        "label": "NLx ingest",
        "engine": "local",
        "target": "core.ops_targets:ingest_nlx",
        "params": [Param("companies", kind="names", default=[]), _TRACK],
    },
    "add-names": {
        "label": "Add companies from pasted text",
        "engine": "local",
        "target": "discovery.paste_ingest:add_names",
        # `names` is the confirmed LIST from /api/names/preview; a raw
        # string is still accepted (add_names parses it) so an older
        # client, or a scripted POST, keeps working.
        "params": [Param("names", kind="raw", default=[]),
                   Param("use_llm", kind="bool", default=False)],
    },
    # The bulk-discovery ops (discover-local, dork, discover-term) are slow
    # (a discover-local pass can hold the single web op slot for half an
    # hour) and low-yield once the roster is saturated, but they stay
    # available as buttons as well as discover.py flags. Every path, bulk
    # or targeted, writes candidates to the Review queue; nothing goes live
    # until a person confirms it.
    "discover-local": {
        "label": "Discover local companies",
        "engine": "local",
        "target": "discovery.local_sourcing:populate_companies",
        "params": [Param("no_dork", "dork", "not", False)],
    },
    "dork": {
        "label": "ATS dork sweep",
        "engine": "local",
        "target": "core.ops_targets:dork_sweep",
        "params": [],
    },
    "discover-term": {
        "label": "Discover companies by term",
        "engine": "local",
        "target": "core.ops_targets:discover_term",
        "params": [Param("term", default=""),
                   Param("no_report", kind="bool", default=False),
                   Param("dry_run", kind="bool", default=False)],
    },
    "score-missions": {
        "label": "Score missions",
        "engine": "local",
        "target": "discovery.local_sourcing:score_missions",
        "params": [Param("rescore", "rescore_all", "bool", False)],
    },
    "add-board": {
        "label": "Add company board",
        "engine": "local",
        "target": "discovery.local_sourcing:add_board",
        "params": [Param("name", default=""), Param("url", default=""),
                   Param("capture", kind="bool")],
    },
    "resolve-leads": {
        "label": "Resolve captured leads",
        "engine": "local",
        "target": "discovery.local_sourcing:resolve_leads",
        "params": [Param("all_leads", kind="bool", default=False),
                   Param("limit", kind="int")],
        "ui": False,      # feeds on capture.py's leads; a CLI step so far
    },
}


def ui_ops():
    """The entries the web UI exposes as buttons (everything not `ui: False`)."""
    return {n: e for n, e in REGISTRY.items() if e.get("ui", True)}


def invoke(name, params=None, *, track=UNSET):
    """Run operation `name` with a front end's params dict; returns what
    the target returns.

    `track` is the track cfg to run against. Left UNSET, it is resolved
    from params["track"] (a track id — the web UI injects the active one)
    or the profile's default track. Pass None explicitly to hand the
    target `t=None` — the CLI's "no --track given" meaning, which each
    target resolves by its own rule.
    """
    entry = REGISTRY[name]
    params = dict(params or {})
    if track is UNSET:
        track = config.UI_TRACKS.get(params.get("track") or config.DEFAULT_TRACK)
    fn = resolve(entry["target"])
    return fn(**build_kwargs(entry["params"], params, track))
