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

  * schema      the SQLite schema, migrations, connect/checkpoint/batch
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

from .companies import (  # noqa: F401
    CAPTURE_ATS, MISS_REASONS, _COMPANY_COLS, _INSERT_ONLY_COLS,
    _NO_BOARD_PREFIXES, _OFFMISSION_MAX_FIT, _OFFMISSION_MIN_JOBS,
    _SHARED_HOST_RE, _board_prefix, _company_index, _domain, _is_crawlable,
    _offmission_volume, _split_url, board_key, company_by_board,
    company_by_host, company_id_by_name, crawlable_companies,
    deactivate_company, dedup_companies, export_companies, get_companies,
    get_company, harvestable_companies, import_companies, mark_harvested,
    miss_counts, miss_family, reactivate_company, recent_miss_names,
    record_crawl_outcome, record_miss, roster_growth, set_company_tag,
    upsert_company,
)
from .jobs import (  # noqa: F401
    TRIAGE_GATES, TRIAGE_OK, _AXIS_TAG, _SCORE_COLS, _TRACK_MATCH_SQL,
    _norm_title, _norm_url, _track_match_arg, backfill_axis_columns,
    combined_score, crawl_seen, dedup_jobs, descriptions_for_company,
    job_exists, join_tracks, mark_desc_checked, ranked_jobs, record_triage,
    remote_admitted, store_body, sync_job_statuses, touch_job, track_set,
    triage_counts, triage_pending, update_job_scores, upsert_job,
)
from .pipeline import (  # noqa: F401
    DISPOSITIONS, RANKING_EXCLUDED_DISPOSITIONS, LIVE_DISPOSITIONS,
    APPLIED_DISPOSITIONS, PIPELINE_FIELDS, OUTCOME_REASONS, FIT_BANDS,
    set_job_status, _resolve_job, set_disposition, get_pipeline,
    update_pipeline_fields, _fit_band, conversion_report, followups_due,
)
from .review import (  # noqa: F401
    _name_key, mark_pending, is_confirmed_company, _PENDING_FIELDS,
    pending_companies, confirm_company, reject_company, block_name,
    blocked_name_keys,
)
from .schema import (  # noqa: F401  (re-exported: store.connect etc.)
    _SCHEMA, _INDEXES, _MIGRATIONS, _RENAMED_COLUMNS, _DROPPED_COLUMNS,
    _ensure_columns, _migrate_tags, BUSY_TIMEOUT_S, connect, checkpoint,
    _BATCHING, _commit, batch,
)
