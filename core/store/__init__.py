"""
One SQLite store (data/jobs.db) shared by every track: a `companies` table
(with a cached mission score and scope tags) and a `jobs` table (per-job
scores, dedup state, track membership). `jobs.track` is a comma-separated
SET, so a posting that belongs to two tracks is ONE row visible to both —
see track_set() and the LIKE-based filters that read it.

Design (merged from both development tracks):
  * The company row carries the mission judgment once, so individual jobs
    inherit it instead of paying a per-job mission LLM call — "the company
    list simplifies the job list."  (local-clinical insight)

Layout: this module is the single import surface (``from core import
store``; ``store.X``), and holds the roster, crawl-scheduling, jobs and
ranking code itself. Three seams live in sibling modules and are
re-exported below, so no caller has to know which file a name is in:

  * store_schema    the SQLite schema, migrations, connect/checkpoint/batch
  * store_review    the review queue (pending / confirm / reject / blocklist)
  * store_pipeline  dispositions, application tracking, follow-ups, conversion

Those siblings never import this module at load time (it imports them), so
a name they need from here is imported inside the function that uses it.
"""

import math
import re
from datetime import datetime, timedelta

import config

import tags

from .schema import (  # noqa: F401  (re-exported: store.connect etc.)
    _SCHEMA, _INDEXES, _MIGRATIONS, _RENAMED_COLUMNS, _DROPPED_COLUMNS,
    _ensure_columns, _migrate_tags, BUSY_TIMEOUT_S, connect, checkpoint,
    _BATCHING, _commit, batch,
)
from .review import (  # noqa: F401
    _name_key, mark_pending, is_confirmed_company, _PENDING_FIELDS,
    pending_companies, confirm_company, reject_company, block_name,
    blocked_name_keys,
)
from .pipeline import (  # noqa: F401
    DISPOSITIONS, RANKING_EXCLUDED_DISPOSITIONS, LIVE_DISPOSITIONS,
    APPLIED_DISPOSITIONS, PIPELINE_FIELDS, OUTCOME_REASONS, FIT_BANDS,
    set_job_status, _resolve_job, set_disposition, get_pipeline,
    update_pipeline_fields, _fit_band, conversion_report, followups_due,
)


def combined_score(fit, mission):
    """Geometric mean sqrt(fit * mission) of the resume-fit and company
    mission scores (both 0..1).

    >>> combined_score(0.25, 0.64)
    0.4
    >>> combined_score(1.0, 1.0)
    1.0

    Floats are compared at a stated precision, never by their full repr —
    the house rule for any numeric doctest:

    >>> round(combined_score(0.9, 0.2), 4)
    0.4243
    >>> round(combined_score(0.5, 0.5), 4)
    0.5

    Those two lines are the point of the geometric mean: it punishes
    imbalance, so a strong fit at a weak-mission company (0.42) ranks below
    a job that is merely solid on both axes (0.50).

    A missing factor is unranked, NOT zero — a job is only scored once both
    axes are known:

    >>> combined_score(None, 0.9) is None
    True
    >>> combined_score(0.9, None) is None
    True

    Negative input is out of domain and yields None rather than a
    ``ValueError`` from ``sqrt`` or a bogus positive from sqrt(-a * -b):

    >>> combined_score(-0.5, 0.5) is None
    True
    >>> combined_score(-0.5, -0.5) is None
    True

    Zero is a legitimate score and stays zero:

    >>> combined_score(0.0, 0.9)
    0.0
    """
    if fit is None or mission is None:
        return None
    if fit < 0 or mission < 0:
        return None
    return math.sqrt(fit * mission)


# --------------------------------------------------------------------------- #
#  Track membership                                                            #
# --------------------------------------------------------------------------- #
#
# jobs.track holds a comma-separated SET of track names, not one name: the
# same posting can belong to several tracks (a neural-company job in your
# area is both local and neural material), and one store now serves every
# track. Same shape as companies.tags, and matched the same way in SQL.

def track_set(value):
    """The set of track names in a stored `track` value ('' / None -> set())."""
    return {t.strip() for t in (value or "").split(",") if t.strip()}


def join_tracks(tracks):
    """Canonical stored form for a set of track names (sorted, comma-joined)."""
    return ",".join(sorted(t for t in tracks if t)) or None


# SQL fragment + arg for "this row belongs to track ?" — the comma-delimited
# LIKE that get_companies already uses for tags.
_TRACK_MATCH_SQL = "(',' || COALESCE(j.track,'') || ',') LIKE ?"


def _track_match_arg(track):
    return f"%,{track},%"


# --------------------------------------------------------------------------- #
#  Companies                                                                   #
# --------------------------------------------------------------------------- #

_COMPANY_COLS = (
    "name", "ats", "slug", "wd_tenant", "wd_pod", "wd_site", "careers_url",
    "local_job_count", "total_job_count", "mission_tier",
    "mission_score", "mission_reason", "tags", "source", "active",
    "last_probed", "notes", "created_at", "miss_reason", "miss_at",
)

# Columns an upsert may write on INSERT but must never overwrite on UPDATE:
# created_at is the row's birth stamp, so a re-probe of a known company must
# leave it (and a legacy NULL) alone.
_INSERT_ONLY_COLS = ("created_at",)


def upsert_company(conn, c):
    """Insert or update a company by name. `c` is a dict of column->value.

    `tags` merge instead of overwrite: a company discovered by the local
    sourcing pass ("nc_local") and later by BCI discovery ("neural") keeps
    both scopes.

    A new row is stamped with `created_at`, and re-upserting the same name
    never moves that stamp -- it is the roster's birth record, not a
    last-touched field (`last_probed` is that one, and it does move):

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "Acme", "ats": "lever"})
    >>> born = conn.execute("SELECT created_at FROM companies").fetchone()[0]
    >>> _ = upsert_company(conn, {"name": "Acme", "ats": "greenhouse"})
    >>> conn.execute("SELECT created_at FROM companies").fetchone()[0] == born
    True

    Writing a board onto a row clears any miss recorded against it: a row
    that has an `ats` is a company, not a miss (see record_miss).

    >>> _ = record_miss(conn, "Zeta", "no-board-found")
    >>> _ = upsert_company(conn, {"name": "Zeta", "ats": "ashby", "active": 1})
    >>> conn.execute("SELECT miss_reason, miss_at FROM companies "
    ...              "WHERE name='Zeta'").fetchone()[:]
    (None, None)
    """
    c = {**c, "last_probed": c.get("last_probed") or datetime.now().isoformat()}
    c.setdefault("created_at", datetime.now().isoformat())
    # Drop None-valued keys: an upsert must never erase an existing value
    # (e.g. a failed/keyless mission-scoring pass writing mission_score=None
    # over a previously scored company). Inserts still get NULL defaults.
    c = {k: v for k, v in c.items() if v is not None}
    old = conn.execute("SELECT tags FROM companies WHERE name=?",
                       (c["name"],)).fetchone()
    if old and old["tags"]:
        merged = set(t for t in old["tags"].split(",") if t)
        merged |= set(t for t in (c.get("tags") or "").split(",") if t)
        c["tags"] = ",".join(sorted(merged))
    cols = [k for k in _COMPANY_COLS if k in c]
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{k}=excluded.{k}" for k in cols
                        if k != "name" and k not in _INSERT_ONLY_COLS)
    conn.execute(
        f"INSERT INTO companies ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(name) DO UPDATE SET {updates}",
        [c[k] for k in cols],
    )
    if c.get("ats"):
        conn.execute("UPDATE companies SET miss_reason=NULL, miss_at=NULL "
                     "WHERE name=? AND miss_reason IS NOT NULL", (c["name"],))
    conn.commit()
    row = conn.execute("SELECT id FROM companies WHERE name=?", (c["name"],)).fetchone()
    return row["id"] if row else None


# --------------------------------------------------------------------------- #
#  Misses                                                                      #
# --------------------------------------------------------------------------- #
#
# A candidate that fails to become a crawlable company used to be printed and
# thrown away, so the same name failed the same way on every run with no
# record of why. It is now kept as an INACTIVE companies row carrying a
# machine-readable `miss_reason` and a `miss_at` retry stamp. Same table on
# purpose: name/source/careers_url/ats are exactly the columns a miss needs
# to record, resolve_leads() already reprocesses boardless inactive rows, and
# `active = 0` is the crawl's existing "do not fetch" switch -- a parallel
# table would duplicate all three and add a second place a name can hide.

# Reason FAMILIES. A stored reason is a family, optionally ':'-qualified with
# the offending platform or error ("ats-unsupported:ukg",
# "fetch-error:ReadTimeout"); miss_counts aggregates on the family so the
# qualifier stays readable without fragmenting the tally.
MISS_REASONS = (
    # no-board-found qualifiers (discovery.sniffer.diagnose_no_board):
    #   :wrong-domain          a candidate resolved to an unrelated company
    #   :domain-unreachable    not one candidate URL answered
    #   :careers-page-no-ats   real job board found, but no known ATS on it
    #   :site-only-no-careers  domain answers, nothing careers-shaped on it
    "no-board-found",   # nothing resolved: sniff, slug-probe and websearch all missed
    "board-dead",       # coordinates detected, but the live fetch returns nothing
    "ats-unsupported",  # a real ATS we recognize but cannot fetch (:platform)
    "no-local-jobs",    # board live and readable, zero openings in [locality]
    "fetch-error",      # the resolution attempt itself raised (:ExceptionName)
)


def miss_family(reason):
    """The family part of a miss reason: the token before any ':' qualifier.

    >>> miss_family("no-local-jobs")
    'no-local-jobs'
    >>> miss_family("ats-unsupported:ukg")
    'ats-unsupported'
    >>> miss_family(None)
    ''
    """
    return (reason or "").split(":", 1)[0]


def record_miss(conn, name, reason, **fields):
    """Record that `name` failed to become a crawlable company, and why.

    The row is always written inactive, so it is invisible to every crawl
    path (all of which read get_companies(active_only=True)):

    >>> conn = connect(":memory:")
    >>> _ = record_miss(conn, "Chiesi USA", "no-local-jobs", ats="greenhouse")
    >>> [c["name"] for c in get_companies(conn, active_only=True)]
    []
    >>> [(c["name"], c["miss_reason"], c["active"])
    ...  for c in get_companies(conn, active_only=False)]
    [('Chiesi USA', 'no-local-jobs', 0)]

    Re-recording the same name updates the reason in place rather than
    growing a second row, so a name that keeps failing stays one worklist
    entry:

    >>> _ = record_miss(conn, "Chiesi USA", "board-dead")
    >>> [(c["name"], c["miss_reason"])
    ...  for c in get_companies(conn, active_only=False)]
    [('Chiesi USA', 'board-dead')]

    An ACTIVE company is never demoted by a miss -- a transient failure while
    re-probing a working board must not drop it out of the roster. Returns
    True when a miss was written, False when it was declined:

    >>> _ = upsert_company(conn, {"name": "Locus", "ats": "lever", "active": 1})
    >>> record_miss(conn, "Locus", "fetch-error:ReadTimeout")
    False
    >>> [c["name"] for c in get_companies(conn, active_only=True)]
    ['Locus']
    """
    row = conn.execute("SELECT active FROM companies WHERE name=?",
                       (name,)).fetchone()
    if row and row["active"]:
        return False
    now = datetime.now().isoformat()
    upsert_company(conn, {**fields, "name": name, "active": 0,
                          "miss_reason": reason, "miss_at": now})
    # upsert_company clears the miss columns whenever an `ats` is written (a
    # row with a board is a company) -- but here the ats is part of the miss
    # record itself ("board-dead" knows which board died), so put them back.
    conn.execute("UPDATE companies SET miss_reason=?, miss_at=? WHERE name=?",
                 (reason, now, name))
    conn.commit()
    return True


def miss_counts(conn):
    """Misses per reason family, biggest first: the "where are we losing
    companies" tally.

    >>> conn = connect(":memory:")
    >>> for n, r in [("a", "no-local-jobs"), ("b", "no-local-jobs"),
    ...              ("c", "ats-unsupported:ukg"),
    ...              ("d", "ats-unsupported:taleo")]:
    ...     _ = record_miss(conn, n, r)
    >>> miss_counts(conn)
    [('ats-unsupported', 2), ('no-local-jobs', 2)]
    """
    rows = conn.execute("SELECT miss_reason FROM companies "
                        "WHERE miss_reason IS NOT NULL").fetchall()
    tally = {}
    for r in rows:
        fam = miss_family(r["miss_reason"])
        tally[fam] = tally.get(fam, 0) + 1
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))


def recent_miss_names(conn, days=14):
    """Names whose miss was recorded within `days`: the set a rerun skips
    instead of re-probing.

    >>> conn = connect(":memory:")
    >>> _ = record_miss(conn, "Fresh", "no-board-found")
    >>> _ = record_miss(conn, "Stale", "no-board-found")
    >>> _ = conn.execute("UPDATE companies SET miss_at=? WHERE name='Stale'",
    ...                  ((datetime.now() - timedelta(days=99)).isoformat(),))
    >>> sorted(recent_miss_names(conn, days=14))
    ['Fresh']

    days=0 disables the skip, so a retry-everything run re-probes the lot:

    >>> recent_miss_names(conn, days=0)
    set()
    """
    if not days:
        return set()
    cutoff = (datetime.now() - timedelta(days=int(days))).isoformat()
    return {r["name"] for r in conn.execute(
        "SELECT name FROM companies WHERE miss_reason IS NOT NULL "
        "AND miss_at IS NOT NULL AND miss_at >= ?", (cutoff,)).fetchall()}


def roster_growth(conn, days=7):
    """How many companies joined the roster in the last `days`.

    Counts `created_at`, not `last_probed`: bulk mission re-scoring rewrites
    last_probed on every row, so only created_at can answer "did the roster
    grow this week".

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "New Co", "ats": "lever"})
    >>> roster_growth(conn, days=7)
    1

    Rows that predate the column (created_at NULL on an upgraded DB) are
    never counted as growth:

    >>> _ = conn.execute("INSERT INTO companies (name) VALUES ('Legacy Co')")
    >>> roster_growth(conn, days=7)
    1

    Neither are misses. A pass that resolves nothing but files fifty
    failures grew the WORKLIST, not the roster, and must not read as growth:

    >>> _ = record_miss(conn, "Nope Bio", "no-board-found")
    >>> roster_growth(conn, days=7)
    1
    """
    cutoff = (datetime.now() - timedelta(days=int(days))).isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM companies "
        "WHERE created_at >= ? AND miss_reason IS NULL",
        (cutoff,)).fetchone()[0]


# --------------------------------------------------------------------------- #
#  Company identity, dedup, and roster CRUD (incl. capture-only rows)          #
# --------------------------------------------------------------------------- #
#
# Some of the best employers cannot be fetched at all: the careers host
# answers a plain request with a bot challenge, or the board is rendered by
# JavaScript on a site with no ATS signature, so discovery left them inactive
# as "no-board-found". For those the person drives the browser (capture.py)
# and the crawler parses only what they saved. Such a company carries
# ``ats = CAPTURE_ATS``: a real roster row, but one no crawl path may fetch.
# crawlable_companies leaves it out, so it never earns an empty streak or a
# fetch error for a board nobody asked.
CAPTURE_ATS = "capture"

# Multi-tenant hosts. A page there says which BOARD it is, not which company
# owns it, so company_by_host trusts a domain-level match against a roster
# row's careers_url only on company-owned hosts; on these it insists on the
# board's own path.
_SHARED_HOST_RE = re.compile(
    r"myworkdayjobs|greenhouse\.io|lever\.co|ashbyhq|smartrecruiters|icims|"
    r"taleo|bamboohr|jazzhr|applytojob|paylocity|workable|polymer\.co|"
    r"gusto\.com|rippling|breezy|recruitee|teamtailor|jobvite|ultipro|"
    r"successfactors|peopleadmin|linkedin|indeed|glassdoor|ziprecruiter", re.I)


def _split_url(url):
    """(host, path) of an http(s) URL, host lower-cased without a leading
    ``www.``; ('', '') for anything else.

    >>> _split_url("https://WWW.Acme.org/careers/jobs?x=1")
    ('acme.org', '/careers/jobs')
    >>> _split_url("jobs.acme.org")
    ('', '')
    """
    m = re.match(r"https?://([^/?#]+)([^?#]*)", (url or "").strip(), re.I)
    if not m:
        return "", ""
    host = re.sub(r"^www\.", "", m.group(1).lower())
    return host, (m.group(2) or "/")


def _board_prefix(path):
    """The first path segment of a careers URL, the piece that names a tenant
    on a shared host ('/axoft/40863' -> '/axoft'); '' for a bare origin."""
    seg = path.strip("/").split("/")[0] if path else ""
    return f"/{seg}" if seg else ""


def company_by_host(conn, url):
    """The roster company whose careers_url (or URL-shaped slug) claims the
    host of `url`, or None. The manual capture path asks this so a page the
    person saved from an employer's own careers site lands under that
    employer's EXISTING row -- id, name, mission score and all -- instead of
    minting a new company from whatever name the page text yields.

    An exact host match wins, and a sibling host on the same company-owned
    domain is accepted too (careers sites live on jobs./careers. subdomains
    while the roster usually holds the www. site):

    >>> conn = connect(":memory:")
    >>> _ = record_miss(conn, "Acme Health", "no-board-found",
    ...                 careers_url="https://www.acmehealth.org/careers/")
    >>> company_by_host(conn, "https://www.acmehealth.org/careers/jobs")["name"]
    'Acme Health'
    >>> company_by_host(conn, "https://jobs.acmehealth.org/search/jobs")["name"]
    'Acme Health'
    >>> company_by_host(conn, "https://jobs.otherhealth.org/") is None
    True

    On a multi-tenant host the domain proves nothing, so the page must sit
    under the board's own first path segment:

    >>> _ = upsert_company(conn, {"name": "Beta Labs",
    ...                           "careers_url": "https://jobs.polymer.co/beta"})
    >>> company_by_host(conn, "https://jobs.polymer.co/beta/40863")["name"]
    'Beta Labs'
    >>> company_by_host(conn, "https://jobs.polymer.co/gamma") is None
    True

    Anything that is not an http(s) URL matches nothing:

    >>> company_by_host(conn, "") is None
    True
    """
    host, path = _split_url(url)
    if not host:
        return None
    shared = bool(_SHARED_HOST_RE.search(host))
    idx = _company_index(conn)
    for c, cpath in idx["by_host"].get(host, ()):
        prefix = _board_prefix(cpath) if shared else ""
        if not prefix or path.lower().startswith(prefix.lower()):
            return c
    if shared:
        return None
    for c, chost in idx["by_domain"].get(_domain(host), ()):
        if chost != host:
            return c
    return None


def board_key(r):
    """The identity of a company row's BOARD, independent of its name: the
    Workday triple, the (ats, slug) pair, or for careers_url-keyed ATSes the
    URL itself. None when the row has no resolvable board. Shared by
    dedup_companies (merging after the fact) and company_by_board (refusing
    the duplicate before it lands).

    careers_url-keyed ATSes: their slug is a shared datacenter host
    (SuccessFactors "performancemanagerN" serves many tenants) or absent,
    and the careers_url IS the board identity. Keying these on slug merged
    Bayer into Sonova (both performancemanager5).

    >>> board_key({"ats": "workday", "wd_tenant": "redhat", "wd_pod": 5, "wd_site": "jobs"})
    ('workday', 'redhat', 5, 'jobs')
    >>> board_key({"ats": "icims", "slug": "globalcareers-sas", "wd_tenant": None})
    ('icims', 'globalcareers-sas')
    >>> board_key({"ats": "custom", "slug": None, "wd_tenant": None,
    ...            "careers_url": "https://x.com/careers/"})
    ('custom', 'https://x.com/careers')
    >>> board_key({"ats": None, "slug": None, "wd_tenant": None}) is None
    True
    """
    if r.get("ats") == "workday" and r.get("wd_tenant"):
        return ("workday", r["wd_tenant"], r.get("wd_pod"), r.get("wd_site"))
    if r.get("ats") in ("successfactors", "peopleadmin", "custom", "wpjson",
                        CAPTURE_ATS):
        u = (r.get("careers_url") or "").rstrip("/").lower()
        return (r["ats"], u) if u else None
    if r.get("ats") and r.get("slug"):
        return (r["ats"], r["slug"])
    return None


def _domain(host):
    """The registrable-ish tail of a host, the piece two sibling careers
    hosts share ('jobs.acme.org' -> 'acme.org')."""
    return ".".join(host.split(".")[-2:])


def _company_index(conn):
    """One scan of the companies table, shaped for the identity lookups
    (company_by_host, company_by_board, dedup_companies) so none of them
    re-walks and re-parses the roster on its own. Built per call, never
    cached: every caller may have just written the row it is about to look
    for.

    Returns a dict:

    * ``rows``       every row as a dict, id order
    * ``by_board``   board_key -> rows with that board, id order
    * ``by_host``    careers host -> [(row, path)], id order, a row's
                     careers_url candidate before its URL-shaped slug
    * ``by_domain``  _domain(host) -> [(row, host)], same order

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "A", "ats": "lever", "slug": "a",
    ...                           "careers_url": "https://www.a.org/jobs/"})
    >>> _ = upsert_company(conn, {"name": "B", "ats": "lever", "slug": "a"})
    >>> idx = _company_index(conn)
    >>> [r["name"] for r in idx["rows"]]
    ['A', 'B']
    >>> [r["name"] for r in idx["by_board"][("lever", "a")]]
    ['A', 'B']
    >>> [(r["name"], p) for r, p in idx["by_host"]["a.org"]]
    [('A', '/jobs/')]
    >>> [(r["name"], h) for r, h in idx["by_domain"]["a.org"]]
    [('A', 'a.org')]
    """
    from collections import defaultdict

    rows = [dict(r) for r in
            conn.execute("SELECT * FROM companies ORDER BY id").fetchall()]
    by_board, by_host, by_domain = defaultdict(list), defaultdict(list), defaultdict(list)
    for c in rows:
        key = board_key(c)
        if key is not None:
            by_board[key].append(c)
        for cand in (c.get("careers_url"), c.get("slug")):
            if not cand or "." not in str(cand):
                continue
            if not re.match(r"https?://", str(cand), re.I):
                cand = f"https://{cand}"
            chost, cpath = _split_url(cand)
            if not chost:
                continue
            by_host[chost].append((c, cpath))
            by_domain[_domain(chost)].append((c, chost))
    return {"rows": rows, "by_board": by_board,
            "by_host": by_host, "by_domain": by_domain}


def company_by_board(conn, row):
    """The existing company row whose board matches `row`'s (see board_key),
    or None. Discovery resolves a pasted or harvested NAME to a board, and a
    name the roster spells differently ("SAS" vs "SAS Institute", "Veeva
    Systems" vs "Veeva", "NVIDIA AI" vs "NVIDIA" — all three re-added on
    2026-09-01) passes the name-keyed already-tracked check and lands as a
    second row on the same board until the next dedup. Checking the board
    before the insert stops the churn at the source."""
    key = board_key(row)
    if key is None:
        return None
    matches = _company_index(conn)["by_board"].get(key)
    return matches[0] if matches else None


def dedup_companies(conn):
    """Merge company rows that point at the SAME board (same ats+slug, or the
    same Workday triple) but were created under different name spellings
    ("IQVIA" vs "Quintiles IMS (IQVIA)") — the name-keyed upsert can't catch
    those, so the crawl fetches one board several times. Jobs are re-pointed to
    the kept row and tags merge, so the merge is lossless. Returns rows merged."""
    groups = _company_index(conn)["by_board"]
    jobcount = {cid: n for cid, n in conn.execute(
        "SELECT company_id, COUNT(*) FROM jobs GROUP BY company_id")}

    def keep_rank(r):
        # Prefer a scored row, then active, then most-referenced, then the
        # shortest (most canonical) name.
        return (r.get("mission_tier") is not None, r.get("active") or 0,
                jobcount.get(r["id"], 0), -len(r.get("name") or ""))

    merged = 0
    for k, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=keep_rank, reverse=True)
        keep, losers = members[0], members[1:]
        tags = set(t for t in (keep.get("tags") or "").split(",") if t)
        for l in losers:
            tags |= set(t for t in (l.get("tags") or "").split(",") if t)
            # Re-point the loser's jobs AND rename them: jobs.company_name
            # is the denormalized display/grouping key (ranked_jobs groups
            # and the digest prints by it), so a merge that only moved
            # company_id left "Red Hat" and "Red Hat (IBM subsidiary, RTP
            # HQ)" as two companies in every ranking after the 2026-09-01
            # dedup, though both pointed at company 85.
            conn.execute("UPDATE jobs SET company_id=?, company_name=? "
                         "WHERE company_id=?",
                         (keep["id"], keep["name"], l["id"]))
            conn.execute("DELETE FROM companies WHERE id=?", (l["id"],))
        active = 1 if any(m.get("active") for m in members) else (keep.get("active") or 0)
        conn.execute("UPDATE companies SET tags=?, active=? WHERE id=?",
                     (",".join(sorted(tags)) or None, active, keep["id"]))
        merged += len(losers)
        print(f"    {keep['name'][:30]:30} <- merged {len(losers)}: "
              + ", ".join(l["name"][:20] for l in losers))
    # Realign every linked job with its company's current name — this
    # catches rows renamed by earlier (name-blind) merges and rows whose
    # ingest path spelled the company its own way ("BD (Becton Dickinson)"
    # linked to company "BD").
    realigned = conn.execute(
        "UPDATE jobs SET company_name=(SELECT name FROM companies "
        "WHERE companies.id=jobs.company_id) WHERE company_id IN "
        "(SELECT id FROM companies) AND company_name IS NOT "
        "(SELECT name FROM companies WHERE companies.id=jobs.company_id)"
    ).rowcount
    if realigned:
        print(f"    {realigned} job row(s) renamed to their company's name")
    conn.commit()
    return merged


def dedup_jobs(conn):
    """Collapse job rows that are the SAME posting under different ids: same
    company, same URL modulo scheme/query/fragment (_norm_url), same
    normalized title. upsert_job's re-key only catches an EXACT URL match,
    so a fetcher that emitted the same posting with a different query string
    (iCIMS `?in_iframe=1` vs `?hub=9&in_iframe=1`, 12 SAS pairs in the
    2026-09-01 store) under a second id namespace slipped past it, and the
    pair then double-ranked and double-spent deep-verify.

    Two guards keep this from eating distinct postings. Title must match —
    some custom boards give several DISTINCT postings one landing URL (see
    upsert_job). And the ids' per-posting tail (the board's own requisition
    number, "..._42453") must match too: Greenhouse companies whose stored
    URL is a shared careers landing page (butterflynetwork.com/careers?
    gh_jid=N) reduce to one URL for every job, and a title reposted under a
    fresh requisition (a second office, a re-opened req) is a separate
    posting, not a duplicate — the dry run without this guard would have
    merged three such Butterfly Network pairs.

    Keeps, per group: a dispositioned row over an undispositioned one, then
    an open row over a closed one, then the earliest first_seen (the row
    whose history is longest). Returns the number of rows deleted."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in conn.execute(
            "SELECT job_id, company_id, url, title, disposition, status, "
            "first_seen FROM jobs WHERE company_id IS NOT NULL "
            "AND url IS NOT NULL AND url != ''"):
        key = (r["company_id"], _norm_url(r["url"]), _norm_title(r["title"]),
               r["job_id"].rsplit("_", 1)[-1])
        if key[1] and key[2]:
            groups[key].append(dict(r))

    def keep_rank(r):
        return (r.get("disposition") is not None,
                (r.get("status") or "open") == "open",
                # earliest first: ISO strings sort by time, so negate via
                # tuple ordering by sorting descending on the inverse
                "" if not r.get("first_seen") else r["first_seen"])

    deleted = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        # Highest disposition/open rank wins; among equals the EARLIEST
        # first_seen (min) wins, so sort ascending on first_seen and
        # descending on the two flags.
        members.sort(key=lambda r: (not keep_rank(r)[0], not keep_rank(r)[1],
                                    keep_rank(r)[2]))
        keep, losers = members[0], members[1:]
        for l in losers:
            conn.execute("DELETE FROM jobs WHERE job_id=?", (l["job_id"],))
        deleted += len(losers)
        print(f"    {(keep['title'] or '')[:40]:40} kept {keep['job_id'][:28]}"
              f" <- dropped {', '.join(l['job_id'][:28] for l in losers)}")
    conn.commit()
    return deleted


def export_companies(conn, path):
    """Dump the company roster to JSON — the shareable/bootstrap artifact
    that replaced config.py's seed lists. Secrets-free by construction."""
    import json
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM companies ORDER BY name").fetchall()]
    for r in rows:
        r.pop("id", None)          # ids are per-database
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    return len(rows)


def import_companies(conn, path):
    """Upsert companies from an export_companies JSON file (idempotent;
    tags merge, existing mission scores survive None fields)."""
    import json
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    n = 0
    for r in rows:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        r.pop("id", None)
        upsert_company(conn, r)
        n += 1
    return n


def set_company_tag(conn, name, tag, add=True):
    """Add or remove one scope tag on a company (case-insensitive name
    match). Returns the company's new comma-joined tag string ('' when the
    last tag was removed), or None if no such company exists."""
    row = conn.execute(
        "SELECT id, tags FROM companies WHERE lower(name)=lower(?)",
        (name,)).fetchone()
    if not row:
        return None
    tags = {t for t in (row["tags"] or "").split(",") if t}
    (tags.add if add else tags.discard)(tag)
    val = ",".join(sorted(tags)) or None
    conn.execute("UPDATE companies SET tags=? WHERE id=?", (val, row["id"]))
    conn.commit()
    return val or ""


def get_company(conn, company_id):
    """One company row by id, or None."""
    if not company_id:
        return None
    row = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    return dict(row) if row else None


def company_id_by_name(conn, name):
    """Resolve a company name to its id (case-insensitive exact match), or
    None if the store has no such company. Used to link externally-ingested
    jobs to their vetted company row so they inherit its mission score."""
    if not name:
        return None
    row = conn.execute(
        "SELECT id FROM companies WHERE lower(name) = lower(?) LIMIT 1",
        (name,)).fetchone()
    return row["id"] if row else None


def get_companies(conn, active_only=True, missions=None, tag=None):
    """Companies, optionally filtered by mission tier(s) and/or scope tag."""
    q = "SELECT * FROM companies"
    conds, args = [], []
    if active_only:
        conds.append("active = 1")
    if missions:
        conds.append(f"mission_tier IN ({','.join('?' for _ in missions)})")
        args += list(missions)
    if tag:
        # tags is a comma-joined token list; match the token exactly.
        conds.append("(',' || COALESCE(tags,'') || ',') LIKE ?")
        args.append(f"%,{tag},%")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY mission_score DESC, local_job_count DESC"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


# --------------------------------------------------------------------------- #
#  Crawl scheduling (dormancy)                                                 #
# --------------------------------------------------------------------------- #
#
# A crawl of the local roster spent most of its wall clock on boards that
# never pay: 181 of 300 active companies had produced zero jobs ever, and
# three high-volume boards produced hundreds of rows nobody would apply to
# (a state health agency: 663 local jobs, best fit 0.15; a games studio;
# a health startup: 30 jobs, best fit 0.03). Deactivating them by hand is
# wrong -- a silent board can start hiring -- so they go DORMANT instead:
# still crawled, just weekly rather than every run.
#
# Two ways in, both reversible by the board itself:
#   * empty streak -- `dormant_after` consecutive DAYS returning nothing;
#   * off-mission volume -- >= 30 jobs stored and a best fit under 0.20.
# Watched companies are exempt from both: the watch tag means "tell me the
# moment anything opens here", which a weekly cadence would break.

# Off-mission volume rule. A board this big that has never scored above
# this is not a scoring accident, it is the wrong employer for the profile.
_OFFMISSION_MIN_JOBS = 30
_OFFMISSION_MAX_FIT = 0.20


def _offmission_volume(conn, company_id):
    """True when this company has stored >= 30 jobs and its BEST resume fit
    is still under 0.20 -- the high-volume off-mission board pattern. A NULL
    max (nothing scored yet) is missing data, not a verdict, so it fails."""
    # Scored rows only: the harvester stores every posting on a board
    # unscored, and 500 unjudged rows next to five scored ones say nothing
    # about the employer.
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(resume_fit_score) AS best FROM jobs "
        "WHERE company_id = ? AND resume_fit_score IS NOT NULL",
        (company_id,)).fetchone()
    return bool(row and row["n"] >= _OFFMISSION_MIN_JOBS
                and row["best"] is not None
                and row["best"] < _OFFMISSION_MAX_FIT)


def record_crawl_outcome(conn, company_id, n_jobs, err=None,
                         dormant_after=4, dormant_days=7):
    """Stamp one company's crawl result and re-decide its crawl_state.

    `n_jobs` is what the board returned for this track (already location
    filtered), `err` the fetch exception if any. Returns the row's new
    crawl_state.

    Rules, in order:
      * a fetch ERROR is neutral -- a 503 or a timeout is our problem, not
        evidence the board is dead, and counting it would retire companies
        during a network wobble;
      * `n_jobs == 0` grows empty_streak, but only ONCE PER CALENDAR DAY:
        several tracks (and a re-run after a crash) hit the same board on
        the same day, and three runs in one afternoon must not read as
        three empty days;
      * `n_jobs > 0` resets the streak and wakes a dormant row;
      * either dormancy rule (streak, off-mission volume) parks the row at
        now + `dormant_days`.

    Watched companies, and rows the user switched 'off', are left alone.
    """
    row = conn.execute(
        "SELECT id, tags, crawl_state, empty_streak, last_crawled_at "
        "FROM companies WHERE id = ?", (company_id,)).fetchone()
    if not row:
        return None
    state = row["crawl_state"] or "active"
    if err is not None:
        return state

    now = datetime.now()
    stamp = now.isoformat()
    streak = row["empty_streak"] or 0
    sets = {"last_crawled_at": stamp}

    if n_jobs:
        streak = 0
        sets["empty_streak"] = 0
        sets["last_nonempty_at"] = stamp
        if state == "dormant":                     # the board woke up
            state = "active"
            sets["next_crawl_at"] = None
    else:
        same_day = (row["last_crawled_at"] or "")[:10] == stamp[:10]
        if not same_day:
            streak += 1
            sets["empty_streak"] = streak

    watched = "watch" in {t for t in (row["tags"] or "").split(",") if t}
    if state != "off" and not watched:
        if streak >= dormant_after or _offmission_volume(conn, company_id):
            state = "dormant"
            sets["next_crawl_at"] = (now + timedelta(days=dormant_days)
                                     ).isoformat()

    sets["crawl_state"] = state
    conn.execute(
        f"UPDATE companies SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
        [*sets.values(), company_id])
    conn.commit()
    return state


def _is_crawlable(company, now=None):
    """Does this company row come up for a crawl right now? A NULL
    crawl_state reads as 'active' (rows that predate the column), a dormant
    row only once its next_crawl_at has passed, an 'off' row never."""
    state = company.get("crawl_state") or "active"
    if state == "active":
        return True
    if state == "dormant":
        return (company.get("next_crawl_at") or "") <= (
            now or datetime.now().isoformat())
    return False


def crawlable_companies(conn, tag=None):
    """The active companies due for a crawl: everything except the dormant
    rows whose weekly slot has not come round yet. What build_sources and
    sync_status_all fetch, in place of every active row.

    Review candidates are `active = 0`, so they are never fetched -- the
    whole point of the queue is that an unconfirmed guess costs nothing:

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, mark_pending(
    ...     {"name": "Guess", "ats": "lever", "slug": "guess"}))
    >>> crawlable_companies(conn)
    []

    A capture-only company (ats = CAPTURE_ATS) is active and on the roster,
    but there is no board to fetch -- the person saves its pages by hand --
    so it is never handed to a fetcher, and never earns an empty streak:

    >>> _ = upsert_company(conn, {"name": "Saved By Hand", "ats": CAPTURE_ATS,
    ...                           "careers_url": "https://jobs.x.org/"})
    >>> crawlable_companies(conn)
    []
    """
    now = datetime.now().isoformat()
    return [c for c in get_companies(conn, active_only=True, tag=tag)
            if c.get("ats") != CAPTURE_ATS and _is_crawlable(c, now)]


# miss_reason families that mean there is nothing at the board's address.
# Everything else (inactive, dormant, pending review, off-mission, even
# 'no-local-jobs') still HAS a board, and the harvester pulls it.
_NO_BOARD_PREFIXES = ("board-dead", "no-board-found")


def harvestable_companies(conn):
    """Every company with a fetchable board, for the background harvester:
    active or not, dormant or not, any tag, any mission score. Skipped only
    when there is no board to fetch (capture rows, no ATS, a dead-board or
    no-board miss) or the name is blocklisted.

    >>> conn = connect(":memory:")
    >>> _ = upsert_company(conn, {"name": "Dormant", "ats": "lever",
    ...                           "slug": "d", "crawl_state": "dormant",
    ...                           "next_crawl_at": "2999-01-01"})
    >>> _ = upsert_company(conn, mark_pending(
    ...     {"name": "Guess", "ats": "greenhouse", "slug": "g"}))
    >>> _ = record_miss(conn, "Dead", "board-dead:ultipro", ats="ultipro",
    ...                 slug="x")
    >>> _ = upsert_company(conn, {"name": "Saved", "ats": CAPTURE_ATS,
    ...                           "careers_url": "https://j.x.org/"})
    >>> sorted(c["name"] for c in harvestable_companies(conn))
    ['Dormant', 'Guess']
    >>> _ = block_name(conn, "Guess")
    >>> sorted(c["name"] for c in harvestable_companies(conn))
    ['Dormant']
    """
    blocked = blocked_name_keys(conn)
    out = []
    for c in get_companies(conn, active_only=False):
        ats = c.get("ats")
        if not ats or ats == CAPTURE_ATS:
            continue
        if (c.get("miss_reason") or "").startswith(_NO_BOARD_PREFIXES):
            continue
        if _name_key(c.get("name") or "") in blocked:
            continue
        out.append(c)
    return out


def mark_harvested(conn, company_id, n_jobs):
    """Stamp a successful whole-board pull (and the board's true size)."""
    conn.execute(
        "UPDATE companies SET last_harvested_at=?, total_job_count=? "
        "WHERE id=?", (datetime.now().isoformat(), n_jobs, company_id))
    _commit(conn)


def reactivate_company(conn, company_id):
    """Undormant one company: back to 'active', streak cleared, no parked
    wake time. The escape hatch for a board the rules retired too eagerly."""
    conn.execute(
        "UPDATE companies SET crawl_state='active', empty_streak=0, "
        "next_crawl_at=NULL WHERE id=?", (company_id,))
    conn.commit()


def deactivate_company(conn, company_id, note=None):
    """Flip one company's `active` switch off, optionally recording why in
    `notes`. The primitive behind scrapers.ops.prune_dead_boards; the
    decision (probe the board, apply the off-mission policy) lives there,
    only the write lives here.

    >>> conn = connect(":memory:")
    >>> cid = upsert_company(conn, {"name": "Gone Co", "ats": "lever",
    ...                             "slug": "gone", "notes": "was fine"})
    >>> deactivate_company(conn, cid, note="deactivated: dead lever board")
    >>> row = get_company(conn, cid)
    >>> row["active"], row["notes"]
    (0, 'deactivated: dead lever board')

    Without a note the existing notes are left alone:

    >>> cid2 = upsert_company(conn, {"name": "Quiet Co", "notes": "keep"})
    >>> deactivate_company(conn, cid2)
    >>> get_company(conn, cid2)["notes"]
    'keep'
    """
    if note is None:
        conn.execute("UPDATE companies SET active=0 WHERE id=?", (company_id,))
    else:
        conn.execute("UPDATE companies SET active=0, notes=? WHERE id=?",
                     (note, company_id))
    _commit(conn)


# --------------------------------------------------------------------------- #
#  Jobs                                                                        #
# --------------------------------------------------------------------------- #

def job_exists(conn, job_id):
    return conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone() is not None


def crawl_seen(conn, job_id):
    """Has a CRAWL already handled this posting? True for a row that
    carries a track label -- the crawl stamps one whether it scored the row
    or stored it unscored under a budget guard. A row the harvester stored
    (no track yet) reads as unseen, so the crawl still gates and scores it:

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "h1", "title": "T",
    ...                       "harvested_at": "2026-09-10T01:00:00"})
    >>> job_exists(conn, "h1"), crawl_seen(conn, "h1")
    (True, False)
    >>> _ = upsert_job(conn, {"job_id": "h1", "title": "T", "track": "local"})
    >>> crawl_seen(conn, "h1")
    True
    """
    row = conn.execute("SELECT track FROM jobs WHERE job_id=?",
                       (job_id,)).fetchone()
    return bool(row and (row["track"] or "").strip())


def descriptions_for_company(conn, company_id):
    """{job_id: description} for a company's stored rows that have a body,
    so a crawl can reuse what the harvester already hydrated instead of
    re-fetching every detail page."""
    if not company_id:
        return {}
    return {r["job_id"]: r["description"] for r in conn.execute(
        "SELECT job_id, description FROM jobs WHERE company_id=? "
        "AND length(COALESCE(description,'')) > 0", (company_id,))}


# --------------------------------------------------------------------------- #
#  Harvest triage (scrapers/triage.py)                                        #
# --------------------------------------------------------------------------- #

# Row verdicts, cheapest gate first. The digest and the pass summary count
# rows by these; `ok` is the only one that puts a row into a track set.
TRIAGE_GATES = ("mission", "title", "anchor", "geo", "exclude", "division",
                "fit")
TRIAGE_OK = "ok"


def triage_pending(conn, company_id=None, limit=None):
    """Open, company-linked rows no crawl has adopted (no track label) and
    triage has not judged yet -- the harvester's unscored material. A row
    triage looked at but could not hydrate stays NULL, so it comes back
    here next pass.

    >>> conn = connect(":memory:")
    >>> cid = upsert_company(conn, {"name": "Acme", "ats": "lever", "slug": "a"})
    >>> for jid, extra in [("h1", {}), ("h2", {"track": "local"}),
    ...                    ("h3", {"status": "closed"}),
    ...                    ("h4", {"triage_status": "title"})]:
    ...     _ = upsert_job(conn, {"job_id": jid, "title": "T",
    ...                           "company_id": cid, **extra})
    >>> record_triage(conn, "h4", "title", "local=title")
    >>> [r["job_id"] for r in triage_pending(conn)]
    ['h1']
    """
    q = ("SELECT j.*, c.name AS company_name_row FROM jobs j "
         "JOIN companies c ON j.company_id = c.id "
         "WHERE j.triage_status IS NULL "
         "AND COALESCE(j.track,'') = '' "
         "AND COALESCE(j.status,'open') != 'closed'")
    args = []
    if company_id is not None:
        q += " AND j.company_id = ?"
        args.append(company_id)
    q += " ORDER BY j.company_id, j.first_seen"
    if limit:
        q += " LIMIT ?"
        args.append(int(limit))
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def record_triage(conn, job_id, status, detail, *, tracks=(), description=None,
                  geo_mode=None, remote_signal=None, scores=None, now=None):
    """Write one row's triage verdict. `tracks` (the track labels the row
    surfaced into) MERGE into the stored set exactly as a crawl's label
    would, so crawl_seen reads the row as handled; `description` fills an
    empty body only; `scores` is a FitResult.as_columns() dict.

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "j", "title": "T", "track": "x"})
    >>> record_triage(conn, "j", "ok", "y=ok", tracks=["y"],
    ...               description="body", scores={"resume_fit_score": 0.5})
    >>> r = conn.execute("SELECT * FROM jobs").fetchone()
    >>> (r["triage_status"], r["triage_detail"], sorted(track_set(r["track"])),
    ...  r["description"], r["resume_fit_score"])
    ('ok', 'y=ok', ['x', 'y'], 'body', 0.5)
    """
    sets = ["triage_status=?", "triage_detail=?", "triaged_at=?"]
    args = [status, detail, (now or datetime.now()).isoformat()]
    if tracks:
        prev = conn.execute("SELECT track FROM jobs WHERE job_id=?",
                            (job_id,)).fetchone()
        merged = track_set(prev["track"] if prev else None) | set(tracks)
        sets.append("track=?")
        args.append(join_tracks(merged))
    if description:
        sets.append("description=COALESCE(NULLIF(description,''), ?)")
        args.append(description[:config.MAX_DESC_CHARS])
    if geo_mode:
        sets.append("geo_mode=COALESCE(geo_mode, ?)")
        args.append(geo_mode)
    if remote_signal:
        sets.append("remote_eligible=1")
        sets.append("remote_signal=COALESCE(remote_signal, ?)")
        args.append(remote_signal)
    if scores:
        for c in _SCORE_COLS:
            sets.append(f"{c}=?")
            args.append(scores.get(c))
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?",
                 [*args, job_id])
    _commit(conn)


def store_body(conn, job_id, description, location=None):
    """Keep a freshly fetched body (and, when the detail page named one,
    the real location) on a row whose verdict is still open, so the next
    pass does not fetch it again. An empty body never blanks a stored one.

    >>> conn = connect(":memory:")
    >>> _ = upsert_job(conn, {"job_id": "j", "title": "T",
    ...                       "location": "2 Locations"})
    >>> store_body(conn, "j", "body", "Durham, NC; Remote")
    >>> r = conn.execute("SELECT description, location FROM jobs").fetchone()
    >>> (r["description"], r["location"])
    ('body', 'Durham, NC; Remote')
    """
    conn.execute(
        "UPDATE jobs SET description=COALESCE(NULLIF(?,''), description), "
        "location=COALESCE(NULLIF(?,''), location) WHERE job_id=?",
        ((description or "")[:config.MAX_DESC_CHARS], location or "", job_id))
    _commit(conn)


def mark_desc_checked(conn, job_id, now=None):
    """Stamp a failed body fetch so the next pass does not retry it at once
    (the same desc_checked_at the backfill ops honour)."""
    conn.execute("UPDATE jobs SET desc_checked_at=? WHERE job_id=?",
                 ((now or datetime.now()).isoformat(), job_id))
    _commit(conn)


def triage_counts(conn, days=None):
    """{verdict: n} over triaged rows, optionally only those judged in the
    last `days` days -- the per-gate funnel the digest shows.

    >>> conn = connect(":memory:")
    >>> for jid, st in [("a", "ok"), ("b", "title"), ("c", "title")]:
    ...     _ = upsert_job(conn, {"job_id": jid, "title": "T"})
    ...     record_triage(conn, jid, st, "")
    >>> triage_counts(conn)
    {'ok': 1, 'title': 2}
    """
    q = ("SELECT triage_status AS s, COUNT(*) AS n FROM jobs "
         "WHERE triage_status IS NOT NULL")
    args = []
    if days:
        q += " AND triaged_at >= ?"
        args.append((datetime.now() - timedelta(days=days)).isoformat())
    q += " GROUP BY triage_status"
    rows = conn.execute(q, args).fetchall()
    order = (TRIAGE_OK, *TRIAGE_GATES)
    return {r["s"]: r["n"] for r in sorted(
        rows, key=lambda r: order.index(r["s"]) if r["s"] in order
        else len(order))}


def upsert_job(conn, j):
    """Insert or refresh a job. Returns True if it was new.

    `first_seen` stays stable across re-runs; scores refresh so the stored
    values always reflect the latest scorer.
    """
    now = datetime.now().isoformat()
    new = not job_exists(conn, j["job_id"])
    if new and j.get("url"):
        # Same posting arriving under a NEW id scheme — a company's ats/
        # tenant changed (Keebler custom_* -> rippling_*) or a fetcher's id
        # format did (Duke sf__<slug> -> sf_<tenant>_<num>). Re-key the
        # existing row instead of inserting a duplicate: dupes double-rank
        # and double-spend deep-verify (17 such URL pairs in the 2026-08-28
        # store). Title must match too — some custom boards give several
        # DISTINCT postings one landing URL, and those must stay separate
        # rows.
        prev = conn.execute(
            "SELECT job_id, title FROM jobs WHERE url=?",
            (j["url"],)).fetchone()
        if (prev is not None
                and (prev["title"] or "").strip().lower()
                == (j.get("title") or "").strip().lower()):
            conn.execute("UPDATE jobs SET job_id=? WHERE job_id=?",
                         (j["job_id"], prev["job_id"]))
            new = False
    remote = j.get("remote_eligible")
    if remote is not None:
        remote = int(bool(remote))
    # `track` is a SET (see track_set): a posting can legitimately belong to
    # several tracks at once — a neural-company job in your area is both
    # local and neural material — so a second track's crawl ADDS its label
    # instead of stealing the row. Merged here in Python; the ON CONFLICT
    # clause below just writes the union.
    track = j.get("track")
    if track and not new:
        prev = conn.execute("SELECT track FROM jobs WHERE job_id=?",
                            (j["job_id"],)).fetchone()
        track = join_tracks(track_set(prev["track"] if prev else None)
                            | track_set(track))
    conn.execute(
        """INSERT INTO jobs
            (job_id, company_id, company_name, title, url, location, track,
             geo_mode, remote_eligible, remote_signal, anchor_signal,
             description, resume_fit_score, fit_reason,
             fit_domain, fit_function, fit_stack, fit_seniority, fit_gates,
             fit_model, posted_at, first_seen, last_seen, status,
             harvested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             title=excluded.title, url=excluded.url, location=excluded.location,
             track=COALESCE(excluded.track, track),
             geo_mode=COALESCE(excluded.geo_mode, geo_mode),
             remote_eligible=COALESCE(excluded.remote_eligible, remote_eligible),
             remote_signal=COALESCE(excluded.remote_signal, remote_signal),
             anchor_signal=COALESCE(excluded.anchor_signal, anchor_signal),
             description=COALESCE(NULLIF(excluded.description,''), description),
             resume_fit_score=COALESCE(excluded.resume_fit_score, resume_fit_score),
             fit_reason=COALESCE(NULLIF(excluded.fit_reason,''), fit_reason),
             fit_domain=COALESCE(excluded.fit_domain, fit_domain),
             fit_function=COALESCE(excluded.fit_function, fit_function),
             fit_stack=COALESCE(excluded.fit_stack, fit_stack),
             fit_seniority=COALESCE(excluded.fit_seniority, fit_seniority),
             fit_gates=COALESCE(excluded.fit_gates, fit_gates),
             fit_model=COALESCE(excluded.fit_model, fit_model),
             posted_at=COALESCE(posted_at, excluded.posted_at),
             last_seen=excluded.last_seen,
             status=excluded.status,
             closed_at=CASE WHEN excluded.status='closed'
                            THEN closed_at ELSE NULL END,
             harvested_at=COALESCE(excluded.harvested_at, harvested_at)""",
        (j["job_id"], j.get("company_id"), j.get("company_name"), j.get("title"),
         j.get("url"), j.get("location"), track, j.get("geo_mode"),
         remote, j.get("remote_signal"), j.get("anchor_signal"),
         j.get("description"),
         j.get("resume_fit_score"), j.get("fit_reason"),
         j.get("fit_domain"), j.get("fit_function"), j.get("fit_stack"),
         j.get("fit_seniority"), j.get("fit_gates"), j.get("fit_model"),
         j.get("posted_at"), now, now, j.get("status", "open"),
         j.get("harvested_at")),
    )
    _commit(conn)
    return new


# --------------------------------------------------------------------------- #
#  Job status sync, score columns, ranking                                     #
# --------------------------------------------------------------------------- #

def _norm_title(t):
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


def _norm_url(u):
    """Scheme/query/fragment/trailing-slash-insensitive URL key."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    return u.split("#", 1)[0].split("?", 1)[0].rstrip("/")



def touch_job(conn, job_id):
    """Record that a job was just observed live at its source — reopen it and
    refresh last_seen, touching nothing else. For dedupe paths that skip the
    full upsert (e.g. a re-captured LinkedIn card already in the store): the
    sighting must still reset the closed flag and the external-row grace
    clock (see sync_job_statuses), or the next board sync could re-close a
    posting the user just saw live."""
    conn.execute(
        "UPDATE jobs SET status='open', closed_at=NULL, last_seen=? "
        "WHERE job_id=?", (datetime.now().isoformat(), job_id))
    conn.commit()


def sync_job_statuses(conn, company_id, fetched_jobs, track=None,
                      external_grace_days=3):
    """Reconcile ONE company's stored jobs against a live board snapshot
    (`fetched_jobs`: dicts with id/title/url, as returned by
    fetchers.company.fetch_company). Rows matched by job_id, URL, or
    normalized title are (re)marked open and their last_seen touched; rows
    that have vanished from the snapshot are marked closed. Returns
    (n_reopened, n_closed).

    Caller contract: only pass a snapshot from a SUCCESSFUL, non-empty fetch
    — fetchers soft-fail to [] (HTTP 404, non-JSON), which is
    indistinguishable from a genuinely emptied board, so an empty snapshot
    must never close anything (this function no-ops on one).

    Matching depends on where the row's job_id came from:
      * BOARD-NATIVE rows — job_id in the snapshot's own id namespace (same
        "<ats>_<key>_" prefix as some snapshot id) — match by EXACT id only.
        The board is authoritative for its own ids, and boards recycle
        titles across requisitions (Beacon reposts "Algorithm Engineer"
        under a fresh Greenhouse id every cycle), so a title/URL fallback
        would let one live posting shield every dead same-titled req from
        ever closing. Absent id -> closed immediately. A partial fetch
        (e.g. Workday pagination dying mid-board) can close rows
        spuriously, but the next full fetch reopens them (matched rows
        always flip back).
      * EXTERNAL rows (LinkedIn captures, NLx, manual --add, legacy ids
        from a retired fetcher) can never id-match, so they match by
        normalized URL or title instead, and are closed only after
        `external_grace_days` without being seen — a manual --add isn't
        insta-closed just because its title doesn't exactly match a board
        row.
      * When `track` is given, only rows of that track are ever CLOSED
        (matched rows are reopened regardless — they're live on the board).
    """
    if not company_id or not fetched_jobs:
        return (0, 0)
    ids = {j.get("id") for j in fetched_jobs if j.get("id")}
    urls = {u for u in (_norm_url(j.get("url")) for j in fetched_jobs) if u}
    titles = {t for t in (_norm_title(j.get("title")) for j in fetched_jobs) if t}
    # Posting dates piggyback on the sync: every matched row gets its NULL
    # posted_at backfilled from the live snapshot, so the whole store gains
    # real posting dates over normal crawls with zero extra HTTP.
    posted = {}
    for j in fetched_jobs:
        p = j.get("posted_at")
        if not p:
            continue
        for key in (j.get("id"), _norm_url(j.get("url")), _norm_title(j.get("title"))):
            if key:
                posted.setdefault(key, p)
    # "gh_<slug>_123" -> "gh_<slug>_": the id namespace(s) this snapshot
    # covers. First TWO tokens, not rsplit — the per-job tail may itself
    # carry underscores ("wd_amgen_<Title-Slug>_R-250290"). Single-token-tail
    # ids ("custom_<blob>") degrade to a full-id prefix, i.e. those rows only
    # ever close via the grace path — right for the flakiest scraped boards.
    prefixes = {"_".join(i.split("_", 2)[:2]) + "_" for i in ids if "_" in i}
    now = datetime.now().isoformat()
    grace_cutoff = (datetime.now()
                    - timedelta(days=external_grace_days)).isoformat()
    n_reopened = n_closed = 0
    rows = conn.execute(
        "SELECT job_id, url, title, track, status, first_seen, last_seen "
        "FROM jobs WHERE company_id=?", (company_id,)).fetchall()
    for r in rows:
        board_native = any(r["job_id"].startswith(p) for p in prefixes)
        present = (r["job_id"] in ids
                   or (not board_native
                       and (_norm_url(r["url"]) in urls
                            or _norm_title(r["title"]) in titles)))
        if present:
            if (r["status"] or "open") != "open":
                n_reopened += 1
            p = (posted.get(r["job_id"]) or posted.get(_norm_url(r["url"]))
                 or posted.get(_norm_title(r["title"])))
            conn.execute(
                "UPDATE jobs SET status='open', closed_at=NULL, last_seen=?, "
                "posted_at=COALESCE(posted_at, ?) WHERE job_id=?",
                (now, p, r["job_id"]))
            continue
        if track is not None and track not in track_set(r["track"]):
            continue
        if (r["status"] or "open") == "closed":
            continue
        seen = r["last_seen"] or r["first_seen"] or ""
        if board_native or seen < grace_cutoff:   # ISO strings sort by time
            conn.execute(
                "UPDATE jobs SET status='closed', closed_at=? WHERE job_id=?",
                (now, r["job_id"]))
            n_closed += 1
    _commit(conn)
    return (n_reopened, n_closed)


# Fit columns written together by the rescore path (see update_job_scores).
_SCORE_COLS = ("resume_fit_score", "fit_reason", "fit_gates", "fit_model",
               "fit_domain", "fit_function", "fit_stack", "fit_seniority")


def update_job_scores(conn, job_id, cols):
    """Overwrite only the fit columns for one job (used by rescore). `cols` is a
    FitResult.as_columns() dict; any missing key is written NULL, so passing an
    empty/partial dict clears a stale score (an unscorable row drops out of
    ranking)."""
    sets = ", ".join(f"{c}=?" for c in _SCORE_COLS)
    conn.execute(f"UPDATE jobs SET {sets} WHERE job_id=?",
                 [cols.get(c) for c in _SCORE_COLS] + [job_id])
    conn.commit()


# Matches the fit_reason tag summary() writes: "[dom0.45 fun0.72 sta0.55
# sen0.80 gate:geo+embedded] reason". Gates are '+'-joined in the tag.
_AXIS_TAG = re.compile(
    r"\[dom([\d.]+) fun([\d.]+) sta([\d.]+) sen([\d.]+)(?: gate:([^\]]+))?\]")


def backfill_axis_columns(conn):
    """Populate the per-axis columns (fit_domain/function/stack/seniority,
    fit_gates) from the tag already embedded in fit_reason. Offline, no API.
    Only touches rows that have the tag and a NULL fit_domain, and leaves
    resume_fit_score / fit_reason untouched. Rows with no tag ('no
    description; unscored', or old single-scalar reasons) are skipped."""
    rows = conn.execute(
        "SELECT job_id, fit_reason FROM jobs "
        "WHERE fit_domain IS NULL AND fit_reason LIKE '[dom%'"
    ).fetchall()
    n = 0
    for r in rows:
        m = _AXIS_TAG.match(r["fit_reason"] or "")
        if not m:
            continue
        dom, fun, sta, sen, gates = m.groups()
        conn.execute(
            "UPDATE jobs SET fit_domain=?, fit_function=?, fit_stack=?, "
            "fit_seniority=?, fit_gates=? WHERE job_id=?",
            (float(dom), float(fun), float(sta), float(sen),
             (gates.replace("+", ",") if gates else None), r["job_id"]),
        )
        n += 1
    conn.commit()
    print(f"  {n} of {len(rows)} row(s) backfilled from fit_reason tags.")
    return n


def remote_admitted(row, remote_mission_floor):
    """Whether an out-of-area REMOTE `row` (a ranked_jobs row) is still
    worth showing in a location-scoped view.

    A watched company qualifies whatever it scores — watch is the one
    human-curated tag, "show me everything at this employer":

    >>> remote_admitted({"company_tags": "local,watch",
    ...                  "mission_score": 0.05}, 0.85)
    True

    Any other company has to reach `remote_mission_floor` on its own
    judged mission score:

    >>> remote_admitted({"mission_score": 0.9}, 0.85)
    True
    >>> remote_admitted({"mission_score": 0.5}, 0.85)
    False

    A company nobody has scored is not admitted (unknown is not a verdict
    in its favour), and a floor of None turns the score arm off entirely,
    leaving watch as the only way in:

    >>> remote_admitted({"mission_score": None}, 0.85)
    False
    >>> remote_admitted({"mission_score": 0.99}, None)
    False

    A multi-division conglomerate never qualifies on score — see
    tests/test_store.py::TestRemoteAdmission, which patches the profile
    policy the check reads.

    Notes:
        The watch list is hand-maintained and lags the data: 8 starred
        companies produced a third of all good-fit rows while 20 unstarred
        ones had produced at least one, and a remote research-engineer
        posting at fit 0.94 fell out of a location-scoped ranking purely
        for want of a star. ranked_jobs applies this server-side; the web
        UI re-applies it per row, because /api/jobs deliberately ships
        every row and gates on the client.
    """
    if tags.has(row.get("company_tags"), tags.WATCH):
        return True
    if remote_mission_floor is None:
        return False
    if config.is_multi_division(row.get("company_name")):
        return False
    mission = row.get("mission_score")
    return mission is not None and mission >= remote_mission_floor


def ranked_jobs(conn, track=None, limit=None, location_re=None, rank_by="combined",
                allow_geo_modes=None, min_mission=None,
                remote_mission_floor=None, include_closed=False,
                include_dispositioned=False):
    """Jobs joined to company mission. `rank_by="combined"` (default) sorts by
    sqrt(resume_fit * company_mission); `rank_by="fit"` sorts by the résumé-fit
    score alone. Use "fit" for a market where every company shares one mission
    tier (e.g. the local health-tech track), so the near-constant mission
    factor doesn't inflate and compress the ranking. `combined_score` is still
    computed either way, so callers can display it. Jobs missing the ranking
    factor fall to the bottom, ordered among themselves by whatever they have.

    `location_re` (a compiled regex) enforces geography at query time,
    independent of the `track` label: a job whose stored location doesn't
    match is excluded from this search but stays in the shared table. This
    is how the local track keeps out-of-area postings out of its results no
    matter which ingest path stamped them `local-tech`.

    `allow_geo_modes` (an iterable of stored `geo_mode` values, e.g.
    {"remote"}) admits rows that fail `location_re` but whose own geo_mode
    already qualifies them — ONLY at companies `remote_admitted` (above)
    trusts with the exception: the watch list, or a mission score at or
    above `remote_mission_floor`. The machine-set sweep tag never earns it —
    auto-probed boards include slug collisions (an EEG company's row that
    actually points at a global AI board), and an unscoped geo_mode
    exception let 87 remote-anywhere rows into a 534-row local ranking.

    `min_mission` drops jobs at companies we positively know are off-mission
    (effective mission below the floor). Needed when ranking by "fit", which
    ignores the mission factor entirely: an off-mission employer's senior ML
    role can otherwise out-rank on-mission work on function/seniority alone
    (a games studio's rec-sys job at fit 0.40 / mission 0.03). Rows with NO
    mission score — unlinked or unscored companies — are KEPT, so the floor
    only removes what has been judged, never what is merely unknown. The
    multi-division floor is applied first, so a conglomerate's keyword-vetted
    job isn't dropped for its parent's low corporate score.

    Jobs marked closed (status='closed' — vanished from their company's
    board, or probed dead; see sync_job_statuses) are excluded unless
    `include_closed=True`. Jobs the user has dispositioned also leave the
    ranking — applied/interviewing live in the digest's pipeline section,
    rejected/dismissed disappear — except 'saved' (shortlisted), which
    stays visible."""
    q = """
      SELECT j.*, c.mission_tier, c.mission_score, c.tags AS company_tags
      FROM jobs j LEFT JOIN companies c ON j.company_id = c.id
    """
    conds, args = [], []
    if track:
        conds.append(_TRACK_MATCH_SQL)
        args.append(_track_match_arg(track))
    if not include_closed:
        conds.append("COALESCE(j.status,'open') != 'closed'")
    if not include_dispositioned:
        ph = ",".join("?" for _ in RANKING_EXCLUDED_DISPOSITIONS)
        conds.append(f"(j.disposition IS NULL OR j.disposition NOT IN ({ph}))")
        args += list(RANKING_EXCLUDED_DISPOSITIONS)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    if location_re is not None:
        rows = [r for r in rows
                if location_re.search(r.get("location") or "")
                or (allow_geo_modes and r.get("geo_mode") in allow_geo_modes
                    and remote_admitted(r, remote_mission_floor))]
    def _effective_mission(r):
        # A conglomerate's own mission score is ~0.05 (off-mission overall),
        # but a job here already passed the health keyword filter at crawl
        # time — so rank it at the keyword-vetted floor, not the company's
        # score, or its combined rank would be sunk unfairly.
        mission = r.get("mission_score")
        if config.is_multi_division(r.get("company_name")):
            mission = max(mission or 0.0, config.MULTI_DIVISION_MISSION_FLOOR)
        return mission

    for r in rows:
        r["combined_score"] = combined_score(r.get("resume_fit_score"),
                                             _effective_mission(r))
    if min_mission is not None:
        rows = [r for r in rows
                if (m := _effective_mission(r)) is None or m >= min_mission]
    # Primary sort key per rank_by, then the other factors as tiebreaks; None
    # sorts last via the -1 sentinel (all real scores are >= 0).
    primary = "resume_fit_score" if rank_by == "fit" else "combined_score"
    def _k(r):
        vals = (r.get(primary), r.get("combined_score"),
                r.get("resume_fit_score"), r.get("mission_score"))
        return tuple(v if v is not None else -1.0 for v in vals)
    rows.sort(key=_k, reverse=True)
    if limit:
        rows = rows[:int(limit)]
    return rows
