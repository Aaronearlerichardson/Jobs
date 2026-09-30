"""ONE registry of the crawler's operations, shared by every front end.

The web UI (src/dispatch/background.py), the maintenance CLI (run_scraper.py) and the
roster CLI (discover.py) used to each carry their own dispatch table over
the same functions, and they drifted: a helper was lost in a refactor
while the button that called it survived, and the CLI and the UI passed
different defaults to the same call. Now each operation is declared once:

    name -> {label, engine, target, params[, ui]}

  label    what the UI shows on the button
  engine   the crawl engine the op needs ("local" / "sweep"), or None for
           any track. Matched against the active track's profile-
           configured `engine`, never against a user-chosen track id.
  target   the function the op runs. A real reference, so renames, type
           checkers and Nuitka all follow it; a test swaps an op by
           patching this entry (conftest.patch_op).
  params   the op's OpParams model: validates a front end's flat
           {key: value} dict into the target's keyword arguments.
  ui       False hides the op from the web UI (CLI-only shape).

A front end awaits `invoke()` with the op name and a params dict (the
JSON the browser POSTed, or values pulled off argparse) and gets the
target's return value back. Parameter names are
the same on both sides, so a button and a flag are two spellings of one
call.

Notes:
    This package sits above src/crawl (two targets live there) and below
    src/web and the root entry scripts, so nothing the harvester imports
    can load it.
"""

from __future__ import annotations

import asyncio
import inspect
import operator
from typing import Annotated, Any, ClassVar

from pydantic import (AfterValidator, BaseModel, BeforeValidator, ConfigDict,
                      Field, ValidationError, model_validator)

from src import config
from src.crawl import runner, triage
from src.discovery import local_sourcing, paste_ingest
from src.ops import (backfill, ingest, rekey, repair, roster, scoring,
                     status)
from src.validation import drop_blank, error_lines

#: "Not given": `invoke(track=UNSET)` resolves the track from params.
UNSET = object()


class ParamError(ValueError):
    """Params an op does not accept; `lines` holds one 'key: problem'
    entry per bad key."""

    def __init__(self, name: str, lines: list[str]) -> None:
        self.lines = lines
        super().__init__(f"bad parameters for {name!r}: " + "; ".join(lines))


def _omit(key: str | None = None) -> Any:
    """A field left out of the kwargs while absent, so the target's own
    default applies."""
    return Field(None, validation_alias=key, exclude_if=lambda v: v is None)


#: A "skip X" flag delivered as X: no_fit=True -> fit=False.
Negated = Annotated[bool, AfterValidator(operator.not_)]
#: A "skip X" flag for a target whose X=None means "the track's own
#: setting": ticked -> False, otherwise None.
Veto = Annotated[bool | None, AfterValidator(lambda v: False if v else None)]
#: The mirror image, "force X on": ticked -> True, otherwise None.
Force = Annotated[bool | None, AfterValidator(lambda v: True if v else None)]
#: Names as a list or one comma-separated string; blanks dropped.
Names = Annotated[list[str],
                  BeforeValidator(lambda v: v.split(",") if isinstance(v, str) else v),
                  AfterValidator(lambda names: [s for s in names if s])]


class OpParams(BaseModel):
    """One op's params, validated into its target's keyword arguments.

    Fields are named for the target's keywords; `validation_alias` names
    the front-end key where the two differ. Strings are stripped, and a
    None or "" value (a blank form field) counts as absent, so the field's
    default applies:

    >>> class Probe(Tracked):
    ...     top_n: int = Field(15, validation_alias="top")
    ...     fit: Negated = Field(True, validation_alias="no_fit")
    ...     max_workers: int | None = _omit("workers")
    >>> Probe.model_validate({"top": " 7 ", "no_fit": True}).kwargs(None)
    {'top_n': 7, 'fit': False, 't': None}
    >>> Probe.model_validate({"top": "", "workers": 3}).kwargs(None)
    {'top_n': 15, 'fit': True, 'max_workers': 3, 't': None}

    A key the op does not declare, or a value of the wrong type, is an
    error naming the key the front end sent:

    >>> try:
    ...     Probe.model_validate({"top": "x", "top_n": 1})
    ... except ValidationError as e:
    ...     error_lines(e)
    ['top: Input should be a valid integer, unable to parse string as an integer', 'top_n: unknown key']

    `track` is the front end's track id and never reaches the target;
    `kwargs` hands the resolved track config to the keyword `track_kw`
    names ("t"), or its store path ("db_path").
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    track_kw: ClassVar[str | None] = None
    track: str | None = None

    _blank_is_absent = model_validator(mode="before")(drop_blank)

    def kwargs(self, track: config.RuntimeTrack | None) -> dict[str, Any]:
        """The target's keyword arguments, with `track` (a track config or
        None) passed as `track_kw` says."""
        out = self.model_dump(exclude={"track"})
        if self.track_kw == "t":
            out["t"] = track
        elif self.track_kw == "db_path":
            out["db_path"] = track.db_path if track else None
        return out


# ── the ops' params (shared shapes first) ────────────────────────────────
# The CLI passes whatever --track resolved to as `t` (None = the target's
# default-track rule).

class Tracked(OpParams):
    track_kw: ClassVar[str | None] = "t"


class TopN(Tracked):
    top_n: int = Field(15, validation_alias="top")


class Rows(Tracked):
    """A capped pass over stored rows on a worker pool."""
    limit: int | None = None
    max_workers: int | None = _omit("workers")


class Crawl(TopN):
    fit: Negated = Field(True, validation_alias="no_fit")
    commit: Negated = Field(True, validation_alias="preview")
    send: Force = None
    verify: Veto = Field(None, validation_alias="no_verify")
    websearch: Veto = Field(None, validation_alias="no_websearch")
    confirm_cost: bool = False
    max_workers: int = Field(6, validation_alias="workers")
    samples: int | None = _omit()


class Verify(TopN):
    max_workers: int = Field(4, validation_alias="workers")
    force: bool = False


class CheckClosed(Tracked):
    limit: int | None = None
    stale_days: int = 2


class Triage(Rows):
    # Re-queue mode (src.crawl.triage.requeue_rows): report, by default,
    # rows an earlier rule mis-judged; only `requeue_apply` too resets
    # them. See run_scraper.py's --requeue/--requeue-apply flags.
    track_kw = "db_path"
    score_cap: int | None = _omit()
    requeue: bool = False
    requeue_apply: bool = False


class Rescore(Tracked):
    described_only: bool = True
    max_workers: int | None = _omit("workers")


class Reresolve(Tracked):
    limit: int = 50
    days: int | None = None
    families: Names | None = _omit()
    commit: Negated = Field(True, validation_alias="preview")
    max_workers: int | None = _omit("workers")


class RenameSlugBoards(Tracked):
    limit: int | None = None
    commit: bool = Field(False, validation_alias="apply")


class RekeyJobs(Tracked):
    ats: str = ""
    commit: bool = Field(False, validation_alias="apply")


class Prune(Tracked):
    offmission: bool = False


class AddJob(Tracked):
    url: str = ""
    title: str = ""
    company: str = ""
    location: str = ""


class Nlx(Tracked):
    companies: Names = []


class AddNames(OpParams):
    # The confirmed LIST from /api/names/preview; a raw string is still
    # accepted (add_names parses it) so an older client, or a scripted
    # POST, keeps working.
    names: list[str] | str = []
    use_llm: bool = False


class DiscoverLocal(OpParams):
    dork: Negated = Field(True, validation_alias="no_dork")


class DiscoverTerm(OpParams):
    term: str = ""
    no_report: bool = False
    dry_run: bool = False


class ScoreMissions(OpParams):
    rescore_all: bool = Field(False, validation_alias="rescore")


class AddBoard(OpParams):
    name: str = ""
    url: str = ""
    capture: bool | None = _omit()


class ResolveLeads(OpParams):
    all_leads: bool = False
    limit: int | None = _omit()


REGISTRY: dict[str, dict[str, Any]] = {
    # ── the crawl ─────────────────────────────────────────────────────
    "crawl": {
        "label": "Crawl",
        "engine": None,   # one command for every track; dispatches on engine
        "target": runner.run_track,
        "params": Crawl,
    },
    # ── maintenance (any track) ───────────────────────────────────────
    "sync": {
        "label": "Sync statuses",
        "engine": None,
        "target": status.sync_status_all,
        "params": TopN,
    },
    "verify": {
        "label": "Deep-verify top N",
        "engine": None,
        "target": scoring.verify_top_cli,
        "params": Verify,
    },
    "check-closed": {
        "label": "Close dead jobs (probe + dead-board)",
        "engine": None,
        "target": status.check_closed_jobs,
        "params": CheckClosed,
    },
    "triage": {
        "label": "Triage harvested rows",
        "engine": None,
        "target": triage.run,
        "params": Triage,
    },
    "rescore": {
        "label": "Rescore all",
        "engine": None,
        "target": scoring.rescore_all,
        "params": Rescore,
    },
    "backfill-descriptions": {
        "label": "Backfill descriptions",
        "engine": None,
        "target": backfill.backfill_board_descriptions,
        "params": Rows,
    },
    "backfill-axes": {
        "label": "Backfill fit axes",
        "engine": None,
        "target": roster.backfill_axes,
        "params": Tracked,
        "ui": False,      # offline column fill; a CLI repair, not a button
    },
    "reresolve": {
        "label": "Retry unresolved companies",
        "engine": None,
        "target": repair.reresolve_misses,
        "params": Reresolve,
    },
    "rename-slug-boards": {
        "label": "Rename slug-named boards",
        "engine": None,
        "target": repair.rename_slug_boards,
        "params": RenameSlugBoards,
    },
    "rekey-jobs": {
        "label": "Re-key a platform's stored job ids",
        "engine": None,
        "target": rekey.rekey_jobs,
        "params": RekeyJobs,
        "ui": False,      # a one-off migration after a spec's id rule changes
    },
    "prune": {
        "label": "Prune dead boards",
        "engine": None,
        "target": roster.prune,
        "params": Prune,
    },
    "dedup": {
        "label": "Dedup companies",
        "engine": None,
        "target": roster.dedup,
        "params": Tracked,
    },
    "add-job": {
        "label": "Add manual job",
        "engine": None,
        "target": ingest.add_manual_job,
        "params": AddJob,
    },
    # ── roster growth (the location-scoped engine's store) ────────────
    "nlx": {
        "label": "NLx ingest",
        "engine": "local",
        "target": roster.ingest_nlx,
        "params": Nlx,
    },
    "add-names": {
        "label": "Add companies from pasted text",
        "engine": "local",
        "target": paste_ingest.add_names,
        "params": AddNames,
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
        "target": local_sourcing.populate_companies,
        "params": DiscoverLocal,
    },
    "dork": {
        "label": "ATS dork sweep",
        "engine": "local",
        "target": roster.dork_sweep,
        "params": OpParams,
    },
    "discover-term": {
        "label": "Discover companies by term",
        "engine": "local",
        "target": roster.discover_term,
        "params": DiscoverTerm,
    },
    "score-missions": {
        "label": "Score missions",
        "engine": "local",
        "target": local_sourcing.score_missions,
        "params": ScoreMissions,
    },
    "add-board": {
        "label": "Add company board",
        "engine": "local",
        "target": local_sourcing.add_board,
        "params": AddBoard,
    },
    "resolve-leads": {
        "label": "Resolve captured leads",
        "engine": "local",
        "target": local_sourcing.resolve_leads,
        "params": ResolveLeads,
        "ui": False,      # feeds on capture.py's leads; a CLI step so far
    },
}


def ui_ops() -> dict[str, dict[str, Any]]:
    """The entries the web UI exposes as buttons (everything not `ui: False`)."""
    return {n: e for n, e in REGISTRY.items() if e.get("ui", True)}


async def invoke(name: str, params: dict[str, Any] | OpParams | None = None, *,
                 track: Any = UNSET) -> Any:
    """Run operation `name` with a front end's params (a dict, or the op's
    model already validated); returns what the target returns. Params the
    op does not accept raise ParamError before anything runs.

    A coroutine target is awaited. A sync one (the ops that only touch the
    store) runs on a worker thread, and once started runs to its end: a
    cancel waits for it (so the next op never overlaps it) and is then
    withdrawn, the call returning or raising what the target did.

    `track` is the track cfg to run against. Left UNSET, it is resolved
    from params["track"] (a track id; the web UI injects the active one)
    or the profile's default track. Pass None explicitly to hand the
    target `t=None`: the CLI's "no --track given" meaning, which each
    target resolves by its own rule.
    """
    entry = REGISTRY[name]
    try:
        args = entry["params"].model_validate({} if params is None else params)
    except ValidationError as e:
        raise ParamError(name, error_lines(e)) from None
    if track is UNSET:
        track = config.UI_TRACKS.get(args.track or config.DEFAULT_TRACK or "")
    target, kw = entry["target"], args.kwargs(track)
    if inspect.iscoroutinefunction(target):
        return await target(**kw)
    done = asyncio.ensure_future(asyncio.to_thread(target, **kw))
    try:
        return await asyncio.shield(done)
    except asyncio.CancelledError:
        await asyncio.wait([done])
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        print(f"  {name}: the stop came too late; a started store op runs to its end")
        return done.result()
