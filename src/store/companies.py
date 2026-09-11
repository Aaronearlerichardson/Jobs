"""The roster: the `companies` table, and everything that decides what
goes in it.

Company rows, the miss log (names that resolved to no board), board
identity and dedup, the roster CRUD every discovery path writes through,
and crawl scheduling (dormancy). The review queue is next door in
review.py; jobs are in jobs.py and this module never touches them.

Split out of store/__init__.py, which had reached 1593 lines around
section banners that were already drawing this line. The two halves turn
out to be genuinely independent -- not one reference crosses between them
-- which is why `dedup_jobs` went to jobs.py rather than staying beside
`dedup_companies`: it reads the jobs table and nothing else.

Never imports store/__init__ at load time (that module imports this one).
"""

import re
from datetime import datetime, timedelta

from src import config

from src import tags

from .schema import _commit, batch, connect  # noqa: F401  (doctests connect)


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
    # no-board-found qualifiers (src.discovery.resolve.sniffer.diagnose_no_board):
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

    >>> from src.store import mark_pending
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

    >>> from src.store import block_name, mark_pending
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
    # Deferred, and so is review.py's one reach back here, so NEITHER
    # module depends on the other at load time and store/__init__ may
    # import them in any order. The roster and the review queue both
    # know what a company name is; that much they genuinely share.
    from .review import _name_key, blocked_name_keys
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
    `notes`. The primitive behind src.ops.maintenance.prune_dead_boards; the
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
