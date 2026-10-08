"""Scoring over stored rows: the unscored-row self-heal, the full rescore,
and the deep verify of the ranking's finalists."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Collection, Mapping
from contextlib import aclosing
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict, cast

from src import config
from src import store
from src.ats.board import board_for_url
from src.ats.board import company as company_fetch
from src.claude import api as claude_api
from src.claude import fit as claude_fit
from src.claude.fit import (DEEP_MARKER, MIN_DESC_CHARS, UNSCORED_CAUSES,
                            FitResult, is_deep_verified, score_resume_fit,
                            unscored_cause, verify_model)
from src.claude.resume import resume_text
from src.config import RuntimeTrack
from src.match.locality import NC_RE
from src.net.parallel import fan_out
from src.net.util import text_from_html
from src.ops.maintenance import _ranked, _t, rewrite_digest, track_writer
from src.rows import JobRow, RankedJob

if TYPE_CHECKING:
    from sqlite3 import Connection

_log = logging.getLogger(__name__)


# How long a REFUSED marker holds off a retry (fit.unscored_cause's
# "refused" class: the model was asked and gave nothing back). A TOO-SHORT
# marker ("short") has no day horizon at all -- see _unscored_due -- because
# the same body can only fail the same way again; only growth changes
# anything, and growth is free to detect (the row's own description length).
UNSCORED_RETRY_DAYS = 30


class _RescoreRow(TypedDict):
    job_id: str
    title: str
    description: str
    location: str | None


def _unscored_marker(cause: str, desc_len: int, when: datetime) -> str:
    """The fit_reason marker for `cause` (fit.unscored_cause's "short" or
    "refused"), carrying what a later pass needs to decide whether another
    attempt is due: the body's length right now (so a changed body is a
    cheap length comparison, not a new column) and the date (the REFUSED
    horizon; unused for "short").

    >>> from datetime import datetime
    >>> _unscored_marker("refused", 645, datetime(2026, 1, 1))
    'unscored:refused:645:2026-01-01'
    """
    return f"unscored:{cause}:{desc_len}:{when.date().isoformat()}"


def _unscored_due(fit_reason: str | None, desc_len: int, now: datetime | None = None) -> bool:
    """Whether a row already carrying an _unscored_marker is due for another
    scoring attempt. No marker at all -- NULL, a real score's tag, or a
    legacy bare "unscored"/"no description; unscored" from before this
    marker existed -- is always due: this is what lets the 2026-09 NC State
    rows (stuck exactly on that legacy bare string) get re-tried once and
    then, if they refuse again, finally marked.

    >>> _unscored_due(None, 645)
    True
    >>> _unscored_due("unscored", 645)
    True

    A REFUSED row is due once its body's length has moved at all (grown OR
    shrunk -- either means the posting changed, worth a fresh ask) or once
    UNSCORED_RETRY_DAYS have passed at the SAME length:

    >>> _unscored_due("unscored:refused:645:2020-01-01", 900)
    True
    >>> from datetime import datetime
    >>> _unscored_due("unscored:refused:645:" + datetime.now().date().isoformat(),
    ...               645)
    False
    >>> _unscored_due("unscored:refused:645:2020-01-01", 645)
    True

    A TOO-SHORT row is due only once it has grown PAST the scoring
    threshold -- shrinking, or growing but still short, is not:

    >>> _unscored_due("unscored:short:150:2020-01-01", 150)
    False
    >>> _unscored_due("unscored:short:150:2020-01-01", 199)
    False
    >>> _unscored_due("unscored:short:150:2020-01-01", 250)
    True
    """
    # fit_reason for a row self_heal_unscored (or rescore_all) could not
    # score: "unscored:<cause>:<body length when marked>:<date marked>".
    # Distinct from every OTHER fit_reason shape in the store -- a real
    # score's tag always starts "[dom" (FitResult.summary), a deep-verified
    # one always contains "deep:" (verify_top._stale, web.routes._job_json
    # read that substring) -- so this can never be mistaken for either by an
    # existing reader. The cause alternation is fit.UNSCORED_CAUSES itself:
    # fit.unscored_cause decides the vocabulary, this only parses back what
    # _unscored_marker wrote.
    m = re.match(r"^unscored:(" + "|".join(UNSCORED_CAUSES) + r"):(\d+):(\d{4}-\d{2}-\d{2})$",
                 fit_reason or "")
    if not m:
        return True
    cause, marked_len, marked_date = m.group(1), int(m.group(2)), m.group(3)
    if cause == "short":
        return desc_len >= MIN_DESC_CHARS
    if desc_len != marked_len:
        return True
    now = now or datetime.now()
    age_days = (now.date() - datetime.strptime(marked_date, "%Y-%m-%d").date()).days
    return age_days >= UNSCORED_RETRY_DAYS


async def self_heal_unscored(db: store.Writer | Connection, resume: str, track: str | None,
                             max_workers: int = 6) -> int:
    """Self-heal: the fresh-only crawl loop never revisits an already-stored
    job, so a row that was ingested bodyless (unscorable -> NULL score) would
    stay out of the ranking forever even once its description is recovered.
    Score any NULL-score row that now carries a real body (hydrated by
    backfill_board_descriptions, or by an earlier run), on `db` (a
    store.Writer, or a connection: track_writer). Returns #scored.

    A row the scorer STILL can't score is no longer left exactly as found:
    it is stamped with an _unscored_marker (cause from fit.unscored_cause)
    so it is not re-asked every single crawl. Retry policy lives in
    _unscored_due: REFUSED waits UNSCORED_RETRY_DAYS or an immediate body
    change; TOO-SHORT (unreachable through THIS query's own length filter
    today, but shared with rescore_all's sibling selection below) waits for
    growth past fit.MIN_DESC_CHARS. The length recorded in the marker, and
    compared against on the next pass, is the STRIPPED body -- the same
    text score_resume_fit itself measures against MIN_DESC_CHARS -- so a
    body whose padding whitespace changes without its real content
    changing is never mistaken for "the posting changed" by _unscored_due.

    desc_checked_at is ALSO stamped here on a refusal, but it does not
    own the retry clock: _unscored_due reads its date from INSIDE the
    fit_reason marker string, never from this column, so a later stamp by
    an unrelated op (check_closed_jobs' probe, a description backfill)
    can never push the REFUSED horizon out or pull it in. The stamp is
    written anyway only to keep desc_checked_at meaning "something last
    looked at this row and found nothing new" for a human or another op
    reading it -- self_heal_unscored's own population (body >=
    MIN_DESC_CHARS) and the description-backfill ops' (body < their own
    min_len) never overlap, so this stamp can never suppress a backfill
    retry either.

    Notes:
        2026-09 evidence (data/logs/session-*.log): the same 3-7 rows at
        one university board re-entered this query and re-refused on every
        crawl for weeks ("Claude returned no text (stop_reason=refusal)",
        17 occurrences) because a refusal wrote nothing -- NULL score,
        untouched fit_reason -- so this query could never tell a row that
        had just failed apart from one that had never been tried.
    """
    conds, args = store.open_in_track_clause(track)
    conds += ["resume_fit_score IS NULL",
              "length(COALESCE(description,'')) >= ?"]
    args.append(MIN_DESC_CHARS)
    async with track_writer(db=db) as db:
        pending = await db.run(lambda conn: [dict(r) for r in conn.execute(
            "SELECT job_id, title, description, location, fit_reason FROM jobs "
            "WHERE " + " AND ".join(conds), args).fetchall()])
        if not pending:
            return 0
        now = datetime.now()
        due = [r for r in pending if _unscored_due(
            r.get("fit_reason"), len((r.get("description") or "").strip()), now)]
        held = len(pending) - len(due)
        if not due:
            print(f"  self-heal: {held} previously-unscorable job(s) not yet "
                  f"due for retry.")
            return 0
        print(f"  self-heal: scoring {len(due)} newly-described "
              f"job(s) that were previously unscorable"
              + (f" ({held} not yet due for retry)" if held else "") + "...")
        scored = 0
        async for r, res in fan_out(due,
                                    lambda r: score_resume_fit(
                                        r["title"], r.get("description", ""),
                                        location=r.get("location") or ""),
                                    "self-heal scoring", max_workers, with_item=True):
            if res.score is not None:
                await db.run(store.update_job_scores, r["job_id"], res.as_columns())
                scored += 1
                continue
            cause = unscored_cause(res.reason)
            if cause is None:
                continue    # scorer offline, or an unrecognized reason: leave as is
            desc_len = len((r.get("description") or "").strip())
            await db.run(store.update_job_scores, r["job_id"],
                         {"fit_reason": _unscored_marker(cause, desc_len, now)})
            await db.run(store.mark_desc_checked, r["job_id"], now)
            detail = (f"retry in {UNSCORED_RETRY_DAYS}d or when the body changes"
                      if cause == "refused"
                      else f"retry once the body grows past {MIN_DESC_CHARS} chars")
            print(f"    [{cause}] {(r.get('title') or '')[:50]} - {detail}")
        return scored


async def rescore_all(max_workers: int = 6, track: str | None = None,
                      described_only: bool = False, t: RuntimeTrack | None = None) -> int:
    """Re-run resume-fit scoring over every stored job in the track's DB
    (all jobs.track values unless `track` names one). Use after changing the
    resume or the scoring prompt — the normal crawl only scores jobs it
    hasn't seen.

    described_only: only touch rows that have a real JD body. Without it, a
    bodyless row's stale score is cleared to NULL so it drops out of
    ranking; a *described* row that merely fails to parse keeps its score.

    Closed and dispositioned-out jobs are always skipped — no Claude API
    spend on postings that can't surface anyway. A row cleared for having no
    body is marked with self_heal_unscored's own _unscored_marker (cause
    "short") rather than a bare string, so the store has one vocabulary for
    "nothing to score here" everywhere it appears -- this call always runs
    the clearing itself (a rescore is an explicit, one-off ask), so unlike
    self_heal_unscored it does not consult _unscored_due first."""
    t = _t(t)
    resume = resume_text()
    if not resume:
        print("  [!] No resume text - cannot rescore. Set config.RESUME_PATH.")
        return 0
    async with track_writer(t) as db:
        conds, args = store.open_in_track_clause(track)
        if described_only:
            conds.append("length(COALESCE(description,'')) >= ?")
            args.append(MIN_DESC_CHARS)
        q = "SELECT job_id, title, description, location FROM jobs"
        if conds:
            q += " WHERE " + " AND ".join(conds)
        rows = cast(list[_RescoreRow],
                    await db.run(lambda conn: [dict(r) for r in conn.execute(q, args).fetchall()]))
        print(f"  rescoring {len(rows)} job(s) against the current resume...")
        now = datetime.now()

        async def _one(r: _RescoreRow) -> tuple[str, FitResult, str]:
            res = await score_resume_fit(r["title"], r.get("description", ""),
                                         location=r.get("location") or "")
            return r["job_id"], res, r.get("description", "")

        n = 0
        async for jid, res, desc in fan_out(rows, _one, "rescore", max_workers):
            if res.score is None:
                # Unscorable (no real body): clear the stale score so it
                # drops from ranking. A described row that merely failed to
                # parse keeps its score.
                if len((desc or "").strip()) < MIN_DESC_CHARS:
                    await db.run(store.update_job_scores, jid, {
                        "fit_reason": _unscored_marker(
                            "short", len((desc or "").strip()), now)})
                    n += 1
                continue
            await db.run(store.update_job_scores, jid, res.as_columns())
            n += 1
    print(f"  {n} job(s) rescored.")
    return n


async def _live_jd(row: JobRow | RankedJob) -> str:
    """Freshest full JD text for one stored job row, preferring a live
    detail fetch (the platform's own detail endpoint through the board
    engine, then the generic JSON-LD/careers-page extractor)
    over the stored text. Falls back to the
    stored description when the live pull is shorter or fails — the deep
    verify pass must never see LESS text than the first pass did. Lengths
    are compared as readable text: a stored body can still carry markup an
    older reader left in it, which only looks longer.

    Notes:
        Compared raw, the markup won: 62 of 96 sampled Greenhouse rows
        (2026-09-22) handed the verify model stored HTML over the same
        posting's clean live text. 12,101 of 16,400 open Greenhouse rows
        still carried that markup then.
    """
    url = row.get("url") or ""
    text = ""
    try:
        board = board_for_url(url)
        if board:
            text = await board.description_for(url)
        if not text and url:
            text = (await company_fetch.job_page_meta(url))[1]
    except Exception:           # fetcher bug or network: fall back to the stored text
        _log.warning("live description for %s failed", url, exc_info=True)
        text = ""
    stored = row.get("description") or ""
    return text if text and len(text) >= len(text_from_html(stored)) else stored


def _verify_floor_candidates(conn: sqlite3.Connection, t: RuntimeTrack, floor: float,
                             exclude_ids: Collection[str] = ()) -> list[JobRow]:
    """Track `t`'s open triage_status='fit' rows screened at or above
    `floor`, located locally (NC_RE) or stored remote_eligible, best screen
    score first, less `exclude_ids` (the top-N slice): the rows verify_top
    reaches past its top N.

    >>> conn = store.connect(":memory:")
    >>> _ = store.upsert_job(conn, {"job_id": "j1", "title": "T",
    ...                             "track": "local-tech", "location": "Elsewhere",
    ...                             "resume_fit_score": 0.3, "remote_eligible": 1})
    >>> store.record_triage(conn, "j1", "fit", "local-tech=ok")
    >>> _ = store.upsert_job(conn, {"job_id": "j2", "title": "T",
    ...                             "track": "local-tech", "location": "Elsewhere",
    ...                             "resume_fit_score": 0.3})
    >>> store.record_triage(conn, "j2", "fit", "local-tech=ok")
    >>> from pathlib import Path
    >>> t = RuntimeTrack(id="t", db_path=Path("t.db"), track="local-tech")
    >>> [r["job_id"] for r in _verify_floor_candidates(conn, t, 0.25)]
    ['j1']

    Notes:
        A 'fit' row already carries the track label (triage's
        record_triage merges it for 'fit' and 'ok' alike), so the track
        LIKE finds it. The location test reads what triage stored, not the
        ranking's remote_admitted trust rule: this chooses where verify
        calls go, it does not admit rows to the ranking.
    """
    conds, args = store.open_in_track_clause(t.track)
    conds += ["triage_status = 'fit'", "resume_fit_score >= ?"]
    args.append(floor)
    rows = [store.as_job(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE " + " AND ".join(conds)
        + " ORDER BY resume_fit_score DESC", args).fetchall()]
    return [r for r in rows if r["job_id"] not in exclude_ids
            and (NC_RE.search(r.get("location") or "")
                 or r.get("remote_eligible") == 1)]


# Ranks verify_top checks whatever their stored fit; past this, a stale row
# needs the track's verify_floor too. 2026-09-22 top-200 run: ~20 of 43 Opus
# calls went to rows stored at 0.19-0.24 that stayed under 0.35 after the
# deep read (Tempus 0.19 -> 0.18, several NVIDIA 0.20-0.22).
VERIFY_HEAD = 25

# Rows whose deep-verify call had started when the round was cut off past
# config.PASS_BUDGET_S. That call may already be paid for, so this process
# asks for them again only under `force`; they keep their first-pass score.
# Not run state (src/runstate.py): the next web UI op or harvest pass skips
# them too.
_GIVEN_UP: set[str] = set()


def _finalists[R: Mapping[str, object]](ranked: list[R], stale: Callable[[R], bool],
                                        floor: float, force: bool) -> tuple[list[R], int]:
    """The `stale` rows of `ranked` a verify round checks: every one ranked
    above VERIFY_HEAD, past it only those stored at or above `floor` (all
    of them under `force`); and how many stale rows the floor left out.

    >>> rows = [{"resume_fit_score": s} for s in [0.1] * VERIFY_HEAD + [0.2, 0.5]]
    >>> top, skipped = _finalists(rows, lambda r: True, 0.35, False)
    >>> len(top), top[-1], skipped
    (26, {'resume_fit_score': 0.5}, 1)
    >>> len(_finalists(rows, lambda r: True, 0.35, True)[0])
    27
    """
    stale_all = [(i, r) for i, r in enumerate(ranked) if stale(r)]
    top = [r for i, r in stale_all
           if force or i < VERIFY_HEAD or cast(float, r.get("resume_fit_score") or 0) >= floor]
    return top, len(stale_all) - len(top)


async def verify_top(top_n: int = 15, max_workers: int = 4, rounds: int = 2,
                     db: store.Writer | None = None, t: RuntimeTrack | None = None,
                     force: bool = False) -> int:
    """Deep-verify the ranking's FINALISTS before anyone acts on them: for
    each of the current top `top_n` jobs (past rank VERIFY_HEAD, only those
    stored at or above the track's `verify_floor`), PLUS enough
    triage_status='fit' candidates (_verify_floor_candidates: local/remote,
    screened at or above the track's `verify_floor`, ordered by screen
    score descending)
    to fill the SAME top_n budget when the top-N slice itself has fewer
    than top_n rows that need it — not already verified BY THE CURRENT
    verify model (fit_reason carrying the 'deep:' marker and fit_model
    naming fit.verify_model()) — re-fetch the freshest full posting text
    (_live_jd), run fit.verify_fit — which extracts hard requirements before
    re-scoring all axes and gates — and write the verified scores back.
    Demotions can pull new unverified rows into the top, so the pass
    re-ranks and repeats up to `rounds` times. A 'fit' row whose deep score
    reaches the track's digest_min_fit is relabelled 'ok'
    (store.record_triage), as triage would have surfaced it.

    Unverifiable rows (dead URL and no stored body, API down) keep their
    first-pass score untouched; once src.claude's breaker has disabled the
    API for this run the pass stops with ONE line instead of a per-row
    'unverified'. Costs at most top_n x rounds API calls per
    run, and only for rows that changed since their last verification or
    were verified by an older model (fit_model NULL counts as older).
    `force=True` re-verifies every finalist regardless, floor or not, and
    rows an earlier round abandoned past its budget
    (candidates are still capped at top_n; force does not widen the
    round's own budget).

    Notes:
        The floor candidates exist because digest_min_fit keeps a weak
        screen score out of the TOP of the ranking, not out of it: an
        underrated row (2026-09-09: screen 0.16, deep 0.50) can sit where
        top_n never reaches, however many rounds run. They only take the
        slots the top-N slice left unspent, so a busy run costs nothing
        extra.

        The per-row 'unverified' it used to print after the breaker tripped
        cost 121 rows x 2 rounds x 2 runs on 2026-09-09, every live JD
        fetched for nothing.
    """
    t = _t(t)
    current = verify_model()
    done_ids: set[str] = set()   # settled THIS run (verified, or no body to read): never stale again, even under force

    def _stale(r: JobRow | RankedJob) -> bool:
        if r["job_id"] in done_ids or (r["job_id"] in _GIVEN_UP and not force):
            return False
        if force or not is_deep_verified(r.get("fit_reason")):
            return True
        return (r.get("fit_model") or "") != current

    async with track_writer(t, db) as db:
        n_done = 0
        for rnd in range(rounds):
            down = claude_api.api_disabled()
            if down:
                # Tripped before this round (the crawl's screen pass, or an
                # earlier round here): nothing below can score, so say so once
                # instead of fetching every finalist's live JD to print
                # '[?] kept' per row.
                print(f"  [!] deep verify skipped: Claude API disabled for this "
                      f"run ({down})")
                break
            ranked = await db.run(_ranked, t, limit=top_n, with_description=True)
            floor = t.verify_floor
            stale_top, n_skipped = _finalists(ranked, _stale, floor, force)
            if n_skipped:
                print(f"  {n_skipped} stale row(s) ranked "
                      f"{VERIFY_HEAD}-{len(ranked)} below the {floor:.2f} floor "
                      f"left unverified")
            remaining = top_n - len(stale_top)
            candidates: list[JobRow] = []
            if remaining > 0:
                seen_ids = {r["job_id"] for r in ranked}
                floor_rows = await db.run(_verify_floor_candidates, t, floor,
                                          exclude_ids=seen_ids)
                candidates = [r for r in floor_rows if _stale(r)][:remaining]
            todo = stale_top + candidates
            if not todo:
                if rnd == 0:
                    print(f"  deep-verify [{t.track}]: nothing new in the "
                          f"top {top_n} or at/above {floor:.2f} for {current}")
                break
            print(f"  deep-verifying {len(stale_top)} of the top {len(ranked)}"
                  + (f" and {len(candidates)} floor candidate(s) at/above "
                     f"{floor:.2f}" if candidates else "")
                  + f" with {current} (round {rnd + 1}/{rounds}"
                  f"{', forced' if force else ''})...")

            started: set[str] = set()

            async def _one(r: JobRow | RankedJob) -> tuple[JobRow | RankedJob, str | None, FitResult]:
                # The breaker can trip mid-round (2026-09-09: the crawl's FIRST
                # verify call hit an exhausted credit balance). A row the API
                # can no longer score doesn't need its live JD fetched.
                if claude_api.api_disabled():
                    # FitResult.score is declared float, but an unscored
                    # result carries None (src/claude/fit.py does the same).
                    return r, None, FitResult(score=None,
                                              reason="api disabled")
                started.add(r["job_id"])
                text = await _live_jd(r)
                return r, text, await claude_fit.verify_fit(r["title"] or "", text,
                                                 location=r.get("location") or "")

            n_scored = n_crushed = 0
            halted = None
            abandoned: list[JobRow | RankedJob] = []
            async with aclosing(fan_out(
                    todo, _one,
                    lambda r: f"verify {r['company_name']}: {(r['title'] or '')[:40]}",
                    max_workers, budget_s=config.PASS_BUDGET_S,
                    on_abandon=abandoned.append)) as verified:
                async for r, text, res in verified:
                    score = res.score
                    if score is None:
                        halted = claude_api.api_disabled()
                        if halted:
                            # One line for the round, not one '[?] kept' per
                            # finalist. Leaving cancels the rows still
                            # running or queued.
                            break
                        print(f"    [?] kept   {(r['title'] or '')[:46]} - {res.reason}")
                        done_ids.add(r["job_id"])
                        continue
                    # A floor candidate that reaches the track's digest_min_fit
                    # on the deep score surfaces exactly as triage would have
                    # surfaced it first-pass; one that doesn't keeps
                    # triage_status='fit' -- its corrected score is still
                    # recorded either way.
                    promote = r.get("triage_status") == "fit" and score >= t.digest_min_fit
                    await db.run(
                        store.record_verified, r["job_id"], res.as_columns(),
                        text if text and len(text) > len(r.get("description") or "") else None,
                        (r.get("triage_detail") or "") if promote else None, [t.track])
                    done_ids.add(r["job_id"])
                    old = r.get("resume_fit_score")
                    was = f"{old:.2f}" if isinstance(old, float) else "?   "
                    move = f"{was} -> {score:.2f}"
                    flag = "  [DEMOTED]" if isinstance(old, float) and \
                        score < old - 0.15 else ""
                    reason = (res.reason or "").removeprefix(f"{DEEP_MARKER} ")[:90]
                    print(f"    {move}, {r['company_name']}, {(r['title'] or '')[:44]}, "
                          f"{reason}{flag}")
                    n_done += 1
                    n_scored += 1
                    if isinstance(old, float) and score < old - 0.25:
                        n_crushed += 1
            if halted:
                print(f"  [!] deep verify halted: Claude API disabled for this run "
                      f"({halted}); {len(todo) - n_scored} finalist(s) keep their "
                      f"first-pass score")
                break
            if abandoned:
                # A queued row was cancelled, never paid for: the next pass asks.
                paid = {r["job_id"] for r in abandoned} & started
                _GIVEN_UP.update(paid)
                print(f"  [!] deep verify halted: {len(abandoned)} finalist(s) "
                      f"past the {config.PASS_BUDGET_S:g}s budget keep their "
                      f"first-pass score; the {len(paid)} whose call had "
                      f"started are not asked again")
                break
            # Tripwire: the two passes disagreeing WHOLESALE is a calibration or
            # parsing defect, not information. Stop instead of compounding.
            if n_scored >= 5 and n_crushed / n_scored >= 0.8:
                print(f"\n  [!] TRIPWIRE: {n_crushed}/{n_scored} verified rows "
                      f"dropped by >0.25 this round. The deep pass is disagreeing "
                      f"with the screen wholesale — that pattern means a prompt/"
                      f"parsing defect, not 30 bad jobs. Halting further rounds; "
                      f"inspect fit_gates on the demoted rows before trusting "
                      f"this ranking.")
                break
        return n_done


async def verify_top_cli(top_n: int = 15, max_workers: int = 4, t: RuntimeTrack | None = None,
                         force: bool = False) -> int:
    """Standalone verify: deep-verify the current top N in the store (no
    crawl), then rewrite the digest and print the corrected top. `force`
    re-verifies rows the current verify model already checked."""
    t = _t(t)
    async with track_writer(t) as db:
        n = await verify_top(top_n=top_n, max_workers=max_workers, db=db, t=t,
                             force=force)
        await db.run(rewrite_digest, t, top_n,
                     lambda n_open: f"\n  {n} job(s) deep-verified; corrected "
                                    f"top {min(top_n, n_open)}:")
    return n
