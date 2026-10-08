"""
One SQLite store (data/jobs.db) shared by every track: a `companies` table
(with a cached mission score and scope tags) and a `jobs` table (per-job
scores, dedup state, track membership). `jobs.track` is a comma-separated
SET, so a posting that belongs to two tracks is ONE row visible to both --
see track_set() and the LIKE-based filters that read it.

Design (merged from both development tracks):
  * The company row carries the mission judgment once, so individual jobs
    inherit it instead of paying a per-job mission LLM call -- "the company
    list simplifies the job list."  (local-clinical insight)

Layout: this module is the single import surface (``from src import
store``; ``store.X``) and holds no code of its own. Five seams live in
sibling modules and are re-exported below, so no caller has to know which
file a name is in:

  * schema      the SQLite schema, migrations, connect/batch/Writer
  * companies   the roster: company rows, misses, board identity and
                dedup, roster CRUD, crawl scheduling (dormancy)
  * jobs        postings: track membership, upsert/dedup, the harvest
                triage columns, status/score/ranking reads
  * review      the review queue (pending / confirm / reject / blocklist)
  * pipeline    dispositions, application tracking, follow-ups, conversion

companies and jobs were one 1593-line module until the section banners
inside it were taken at their word. They share nothing -- not one
reference crosses between them -- so the split is a seam, not a cut.

Siblings never import this module at load time (it imports them), so a
name one of them needs from another is imported directly from that
sibling.
"""

from __future__ import annotations

from .companies import (  # noqa: F401
    CAPTURE_ATS, MISS_REASONS, _OFFMISSION_MIN_JOBS, _offmission_volume,
    add_board, as_company, board_key, company_by_board, plan_board, realign_job_names,
    company_by_host, company_id_by_name, crawlable_companies,
    deactivate_company, dedup_companies, export_companies, get_companies,
    HARVEST_DEAD_AFTER_DAYS, blocked_keys, blocked_name_keys, get_company, harvestable_companies,
    import_companies, mark_harvest_attempted, mark_harvested,
    miss_counts, miss_family, miss_family_in, reactivate_company, recent_miss_names,
    record_alias, record_crawl_outcome, record_miss, roster_growth, roster_rows,
    upsert_company,
)
from .employers import (  # noqa: F401
    set_board_mission, set_watch,
)
from .health import (  # noqa: F401
    latest_platform_health, platform_health_history, record_platform_health,
)
from .jobs import (  # noqa: F401
    TRIAGE_GATES, TRIAGE_OK, _SCORE_COLS, clear_triage, close_dead_board_jobs,
    combined_score, crawl_seen, dedup_jobs, descriptions_for_company,
    flag_duplicate_jobs, job_exists, join_tracks, mark_desc_checked, merge_jobs, open_in_track_clause,
    ranked_jobs, record_probe_outcome, record_triage, record_verified, remote_admitted,
    retire_stopped, same_posting, store_body, sync_job_statuses, touch_job, track_set,
    triage_counts, triage_pending, update_job_scores, upsert_job,
)
from .pipeline import (  # noqa: F401
    RANKING_EXCLUDED_DISPOSITIONS, PipelineFields, OUTCOME_REASONS, DISMISS_REASONS,
    NOT_A_FIT_SIGNAL, Prior, prior_lookup,
    set_job_status, set_disposition, get_pipeline, update_pipeline_fields,
    conversion_report, followups_due,
)
from .review import (  # noqa: F401
    mark_pending, is_confirmed_company,
    pending_companies, confirm_company, reject_company, block_name,
)
from .schema import (  # noqa: F401  (re-exported: store.connect etc.)
    BUSY_TIMEOUT_S, Writer, as_job, connect, batch,
)
