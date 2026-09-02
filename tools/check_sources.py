#!/usr/bin/env python3
"""Source health probe — what can this crawler actually reach today?

    python tools/check_sources.py                  # everything below
    python tools/check_sources.py --only robots    # one section
    python tools/check_sources.py --roster         # + probe YOUR stored boards
    python tools/check_sources.py --json out.json

`tools/check_boards.py` answers one question well: is each ATS platform's
API still shaped the way its parser expects? It deliberately checks one
sample board per platform and nothing else. This covers the rest of what a
crawl touches, and classifies WHY something is unreachable — the distinction
that decides what you do about it:

  robots     the host's robots.txt asks us not to fetch that path. Not an
             error, not fixable, and not something to route around: the
             crawler honors it (config.RESPECT_ROBOTS), so those postings
             are simply out of scope for automated fetching.
  blocked    reachable but refusing US — 401/403/429/451, a CAPTCHA, an
             anti-bot wall. Often IP- or rate-based, so it may pass on a
             retry or from another network. Says nothing about the parser.
  broken     404/410/5xx/timeout/parse error. A real failure: a dead
             endpoint, a moved API, or a shape change.
  ok         alive, allowed, and returning what we expect.

Sections: robots (policy per host), feeds (aggregators), search (web
search), forums (Discourse), api (keyed government feeds), gated (the hosts
this tool refuses to fetch by design), and --roster (every active board in
your own store — the practical "which of my saved boards died" pass).

Read-only: every probe is a GET or a documented public API call, one per
host, paced. Nothing is written to the store.
"""

import argparse
import contextlib
import io
import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # Windows consoles default to cp1252; the status glyphs are not in it.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import config                                       # noqa: E402
from scrapers.http import SESSION, HEADERS          # noqa: E402
from scrapers.robots import CACHE as ROBOTS         # noqa: E402

OK, BLOCKED, BROKEN, ROBOTS_OFF, SKIPPED = (
    "ok", "blocked", "broken", "robots", "skipped")

EMOJI = {OK: "✅", BLOCKED: "🚧", BROKEN: "❌", ROBOTS_OFF: "🤖", SKIPPED: "⏭️"}

_BLOCKED_RE = re.compile(
    r"\b(401|403|429|451)\b|captcha|cloudflare|forbidden|rate.?limit|"
    r"too many requests|access denied|challenge", re.I)


# A fetcher that declined on POLICY, not on failure. RobotsDisallowed is
# raised (and printed) by the session before any request goes out.
_ROBOTS_RE = re.compile(r"robots\.txt disallows|RobotsDisallowed", re.I)


def verdict(blob, has_rows):
    """Classify a fetcher outcome from its diagnostics. Policy beats
    failure: a robots refusal is not a broken endpoint, and reporting it as
    one sends you off debugging a parser that is working fine."""
    if has_rows:
        return OK
    if _ROBOTS_RE.search(blob or ""):
        return ROBOTS_OFF
    if _BLOCKED_RE.search(blob or ""):
        return BLOCKED
    return BROKEN


def classify(status_code=None, note=""):
    """HTTP status + any diagnostic text -> one of our four verdicts."""
    if _BLOCKED_RE.search(note or ""):
        return BLOCKED
    if status_code is None:
        return BROKEN
    if status_code in (401, 403, 429, 451):
        return BLOCKED
    if status_code >= 400:
        return BROKEN
    return OK


# --------------------------------------------------------------------------- #
#  1. robots.txt policy                                                        #
# --------------------------------------------------------------------------- #
#
# (label, a URL we would REALLY fetch). The path matters: robots.txt rules
# are per-path, so asking "is the host allowed?" is the wrong question —
# greenhouse may allow its API and disallow its HTML board, and only the
# path we actually use tells us whether a crawl is affected.
ROBOTS_TARGETS = [
    # --- ATS APIs the fetchers call -------------------------------------
    ("greenhouse api",   "https://boards-api.greenhouse.io/v1/boards/databricks/jobs"),
    ("greenhouse board", "https://boards.greenhouse.io/databricks"),
    ("lever api",        "https://api.lever.co/v0/postings/veeva"),
    ("lever board",      "https://jobs.lever.co/veeva"),
    ("ashby api",        "https://api.ashbyhq.com/posting-api/job-board/vanta"),
    ("ashby board",      "https://jobs.ashbyhq.com/vanta"),
    ("bamboohr api",     "https://ems.bamboohr.com/careers/list"),
    ("workday cxs",      "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"),
    ("smartrecruiters",  "https://api.smartrecruiters.com/v1/companies/example/postings"),
    ("jazzhr",           "https://example.applytojob.com/apply"),
    ("kula",             "https://careers.kula.ai/precision-neuroscience"),
    ("rippling",         "https://ats.rippling.com/blackrockneurotech/jobs"),
    ("paylocity",        "https://recruiting.paylocity.com/recruiting/jobs/All"),
    ("ultipro",          "https://recruiting.ultipro.com/"),
    ("successfactors",   "https://careers.duke.edu/search/"),
    ("peopleadmin",      "https://unc.peopleadmin.com/postings/search.atom"),
    ("icims",            "https://careers-example.icims.com/jobs/search"),
    # --- aggregator feeds ------------------------------------------------
    ("remoteok",         "https://remoteok.com/api"),
    ("remotive",         "https://remotive.com/api/remote-jobs"),
    ("hn firebase",      "https://hacker-news.firebaseio.com/v0/user/whoishiring.json"),
    ("weworkremotely",   "https://weworkremotely.com/categories/remote-programming-jobs.rss"),
    ("jobicy",           "https://jobicy.com/?feed=job_feed"),
    # --- web search ------------------------------------------------------
    ("duckduckgo html",  "https://html.duckduckgo.com/html/?q=test"),
    ("duckduckgo lite",  "https://lite.duckduckgo.com/lite/?q=test"),
    # --- government / directories ---------------------------------------
    ("careeronestop",    "https://api.careeronestop.org/v1/jobsearch/"),
    ("bciwiki",          "https://bciwiki.org/index.php/Category:Companies"),
    ("builtin",          "https://builtin.com/companies"),
    # --- hosts we refuse to fetch anyway (see GATED) ---------------------
    ("linkedin",         "https://www.linkedin.com/jobs/search/"),
    ("indeed",           "https://www.indeed.com/jobs?q=engineer"),
    ("glassdoor",        "https://www.glassdoor.com/Job/index.htm"),
    ("ziprecruiter",     "https://www.ziprecruiter.com/jobs-search"),
    ("monster",          "https://www.monster.com/jobs/search"),
    ("metacareers",      "https://www.metacareers.com/jobs"),
]


def probe_robots(label, url):
    """What does this host's robots.txt say about the path we'd fetch?"""
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    started = time.monotonic()
    code, note = None, ""
    try:
        # Plain requests, NOT the crawler's SESSION: the session is
        # robots-aware, so on a host with `Disallow: /` it refuses to fetch
        # the very robots.txt we are trying to read, and the probe reports a
        # transport failure for what is actually a policy decision.
        import requests
        r = requests.get(f"{origin}/robots.txt", timeout=12, headers=HEADERS,
                         allow_redirects=True)
        code = r.status_code
        body = r.text if r.status_code < 400 else ""
    except Exception as e:
        note, body = f"{type(e).__name__}: {e}", ""

    # The crawler's own decision, through the same cache the crawl uses.
    try:
        allowed = ROBOTS.allowed(url)
        delay = ROBOTS.crawl_delay(url)
    except Exception as e:
        allowed, delay, note = True, None, note or f"{type(e).__name__}: {e}"

    if note and code is None:
        status, detail = BROKEN, f"robots.txt unreachable: {note[:70]}"
    elif not allowed:
        status = ROBOTS_OFF
        detail = "DISALLOWED for this path"
    else:
        status = OK
        if code is None or code >= 400:
            # RFC 9309: 4xx means no restrictions. Worth saying, not warning.
            detail = f"no robots.txt (HTTP {code}) — unrestricted"
        else:
            detail = "allowed"
        if delay:
            detail += f", crawl-delay {delay:g}s"
    n_rules = sum(1 for ln in (body or "").splitlines()
                  if ln.strip().lower().startswith("disallow:"))
    return {"section": "robots", "name": label, "status": status,
            "detail": detail, "http": code, "rules": n_rules,
            "seconds": round(time.monotonic() - started, 1)}


# --------------------------------------------------------------------------- #
#  2. aggregator feeds                                                         #
# --------------------------------------------------------------------------- #

def widen_keyword_filter():
    """Make `is_relevant()` accept everything, in place.

    Every feed and board fetcher applies the keyword filter INTERNALLY, so
    without this a healthy source that simply doesn't match your search
    reports identically to a dead one. We are measuring the SOURCE here, not
    the profile. (filters.py bound these list objects at import time, so they
    must be mutated rather than rebound — see runner.apply_keyword_focus.)"""
    config.CORE_KEYWORDS[:] = [""]          # "" is a substring of any text
    config.DOMAIN_KEYWORDS[:] = []
    config.SKILL_KEYWORDS[:] = []
    config.INCLUDE_KEYWORDS[:] = [""]
    config.EXCLUDE_PHRASES[:] = []
    config.EXCLUDE_TITLE_PHRASES[:] = []
    config.ACCEPT_REMOTE = True


class _ThreadCapture:
    """A stdout stand-in that routes each thread's writes to its own buffer.

    `contextlib.redirect_stdout` swaps the GLOBAL `sys.stdout` and restores it
    on exit, which is fine sequentially and silently destructive in a thread
    pool: one worker restores the real stdout while another still holds the
    redirect, and every later print lands in a StringIO nobody reads — the
    roster probe ran to completion and printed nothing at all. Installed once
    for a whole threaded section instead, with per-thread buffers.
    """

    def __init__(self, passthrough):
        self._buffers = threading.local()
        self._passthrough = passthrough

    def start(self):
        self._buffers.buf = io.StringIO()

    def take(self):
        buf = getattr(self._buffers, "buf", None)
        self._buffers.buf = None
        return " ".join(buf.getvalue().split()) if buf else ""

    def write(self, s):
        buf = getattr(self._buffers, "buf", None)
        (buf or self._passthrough).write(s)
        return len(s)

    def flush(self):
        self._passthrough.flush()


def _run_fetcher(fn, *a, **kw):
    """Call a fetcher, capturing the diagnostics it prints. Returns
    (rows, note, exception_text).

    Thread-safe when `sys.stdout` is a _ThreadCapture (the roster path);
    falls back to a plain redirect for the sequential sections."""
    cap = sys.stdout if isinstance(sys.stdout, _ThreadCapture) else None
    if cap is not None:
        cap.start()
        try:
            rows = fn(*a, **kw)
            return rows or [], cap.take(), ""
        except Exception as e:
            return [], cap.take(), f"{type(e).__name__}: {e}"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rows = fn(*a, **kw)
        return rows or [], " ".join(buf.getvalue().split()), ""
    except Exception as e:
        return [], " ".join(buf.getvalue().split()), f"{type(e).__name__}: {e}"


def probe_feeds():
    from scrapers.fetchers import (fetch_hnhiring, fetch_remoteok,
                                   fetch_remotive, fetch_rss)

    out = []
    checks = [("remoteok", lambda: fetch_remoteok(max_jobs=50)),
              ("remotive", lambda: fetch_remotive(max_jobs=50)),
              ("hn who-is-hiring", lambda: fetch_hnhiring(max_threads=1))]
    for label, url, loc in config.RSS_FEEDS:
        checks.append((f"rss: {label}",
                       lambda u=url, l=loc: fetch_rss("probe", u, l, max_items=50)))

    for label, call in checks:
        started = time.monotonic()
        rows, note, exc = _run_fetcher(call)
        status = verdict(f"{note} {exc}", bool(rows))
        detail = (f"{len(rows)} postings" if rows
                  else (exc or note or "0 postings, no diagnostic")[:110])
        out.append({"section": "feeds", "name": label, "status": status,
                    "detail": detail, "rows": len(rows),
                    "seconds": round(time.monotonic() - started, 1)})
        time.sleep(1.0)
    return out


# --------------------------------------------------------------------------- #
#  3. web search                                                               #
# --------------------------------------------------------------------------- #
#
# The flakiest source by a wide margin, and the one the crawl spends the most
# wall-clock on. Three layers can each fail independently, so probe each:
#   the DDG endpoints directly | the bounded/cached scrapers.ddg.search() |
#   the full fetch_websearch() (search -> visit results -> parse JSON-LD)

SEARCH_QUERIES = [
    ("plain terms",     'machine learning engineer jobs'),
    ("site: operator",  '"engineer" site:jobs.lever.co'),
    ("quoted boolean",  '("neural" OR "EEG") ("engineer" OR "scientist") site:weworkremotely.com'),
]


def probe_search(deep=False):
    from scrapers.ddg import search as ddg_text

    out = []

    # -- layer 1: the raw endpoints ---------------------------------------
    for label, url in (("ddg html endpoint", "https://html.duckduckgo.com/html/?q=test"),
                       ("ddg lite endpoint", "https://lite.duckduckgo.com/lite/?q=test")):
        started = time.monotonic()
        try:
            r = SESSION.get(url, timeout=20, headers=HEADERS)
            body = r.text[:4000].lower()
            challenged = any(s in body for s in
                             ("captcha", "unusual traffic", "are you a robot",
                              "challenge-platform"))
            status = (BLOCKED if challenged else classify(r.status_code))
            detail = (f"HTTP {r.status_code}"
                      + (", anti-bot challenge in body" if challenged else "")
                      + f", {len(r.content)} bytes")
        except Exception as e:
            status, detail = classify(None, str(e)), f"{type(e).__name__}: {e}"[:110]
        out.append({"section": "search", "name": label, "status": status,
                    "detail": detail,
                    "seconds": round(time.monotonic() - started, 1)})
        time.sleep(1.5)

    # -- layer 2: the wrapper the crawl actually calls ---------------------
    # NB: ddg_text caches hits for 7 days, so a pass here can mean "served
    # from cache". Reported so the result isn't over-read.
    for label, q in SEARCH_QUERIES:
        started = time.monotonic()
        rows, note, exc = _run_fetcher(ddg_text, q, max_results=8)
        elapsed = round(time.monotonic() - started, 1)
        cached = elapsed < 0.5 and rows
        if rows:
            status = OK
            detail = f"{len(rows)} results" + (" (from disk cache)" if cached else "")
        elif exc:
            status, detail = BROKEN, exc[:110]
        else:
            # ddg_text swallows everything and returns [] — by design, but it
            # means "throttled" and "no matches" look identical from here.
            status = BLOCKED
            detail = (f"0 results after {elapsed}s — throttled or "
                      f"wall-budget timeout (ddg_text returns [] either way)")
        out.append({"section": "search", "name": f"ddg_text: {label}",
                    "status": status, "detail": detail, "rows": len(rows),
                    "seconds": elapsed})
        time.sleep(2.0)

    # -- layer 3: the whole pipeline --------------------------------------
    if deep:
        from scrapers.fetchers.websearch import fetch_websearch
        for label, q in SEARCH_QUERIES[1:]:
            started = time.monotonic()
            rows, note, exc = _run_fetcher(
                fetch_websearch, f"probe: {label}", q, max_results=5)
            blob = f"{note} {exc}"
            if rows:
                status, detail = OK, f"{len(rows)} postings parsed from results"
            elif exc:
                status, detail = BROKEN, exc[:110]
            else:
                status = BLOCKED if _BLOCKED_RE.search(blob) else BROKEN
                detail = (note[:110] or
                          "0 postings — no results, or none carried JSON-LD")
            out.append({"section": "search", "name": f"fetch_websearch: {label}",
                        "status": status, "detail": detail, "rows": len(rows),
                        "seconds": round(time.monotonic() - started, 1)})
            time.sleep(2.0)
    return out


# --------------------------------------------------------------------------- #
#  4. forums                                                                   #
# --------------------------------------------------------------------------- #

def probe_forums():
    from scrapers.fetchers.discourse import fetch_discourse
    out = []
    if not config.DISCOURSE_BOARDS:
        return [{"section": "forums", "name": "(none configured)",
                 "status": SKIPPED, "detail": "no [sources].discourse entries",
                 "seconds": 0.0}]
    for label, url, cat in config.DISCOURSE_BOARDS:
        started = time.monotonic()
        rows, note, exc = _run_fetcher(fetch_discourse, label, url, cat)
        status = verdict(f"{note} {exc}", bool(rows))
        detail = (f"{len(rows)} postings" if rows
                  else (exc or note or "0 topics in that category")[:110])
        out.append({"section": "forums", "name": f"{label} (cat {cat})",
                    "status": status, "detail": detail, "rows": len(rows),
                    "seconds": round(time.monotonic() - started, 1)})
        time.sleep(1.0)
    return out


# --------------------------------------------------------------------------- #
#  5. keyed APIs                                                               #
# --------------------------------------------------------------------------- #

def probe_api():
    out = []
    has_key = bool(config.CAREERONESTOP_USER_ID and config.CAREERONESTOP_TOKEN)
    started = time.monotonic()
    if not has_key:
        out.append({"section": "api", "name": "CareerOneStop / NLx",
                    "status": SKIPPED,
                    "detail": "no CAREERONESTOP_USER_ID / _TOKEN set — "
                              "register free at careeronestop.org/Developers",
                    "seconds": 0.0})
        return out
    from scrapers.fetchers.careeronestop import fetch_nlx_company
    rows, note, exc = _run_fetcher(fetch_nlx_company, "Google", max_pages=1)
    status = verdict(f"{note} {exc}", bool(rows))
    detail = (f"{len(rows)} postings" if rows
              else (exc or note or "0 postings returned")[:110])
    out.append({"section": "api", "name": "CareerOneStop / NLx",
                "status": status, "detail": detail, "rows": len(rows),
                "seconds": round(time.monotonic() - started, 1)})
    return out


# --------------------------------------------------------------------------- #
#  6. deliberately gated hosts                                                 #
# --------------------------------------------------------------------------- #

def probe_gated():
    """Hosts the crawler refuses to fetch by policy, not by capability.

    Nothing is requested here — that is the point. This section exists so the
    report is honest about coverage: these sites carry postings we never see
    automatically, and capture.py is the intended route (you browse them
    yourself, signed in as you, and the parser reads the page your browser
    already loaded)."""
    from scrapers.fetchers.company import _GATED_HOST_RE
    hosts = _GATED_HOST_RE.pattern.replace("\\.", ".").split("|")
    return [{"section": "gated", "name": h.strip(), "status": SKIPPED,
             "detail": "never fetched by policy — use capture.py",
             "seconds": 0.0}
            for h in hosts if h.strip()]


# --------------------------------------------------------------------------- #
#  7. your own roster                                                          #
# --------------------------------------------------------------------------- #

def probe_roster(limit=None, workers=8):
    """Probe every ACTIVE board in the store. This is the practical 404 pass:
    discovery imports leave stale slugs behind, companies get acquired, and
    boards move — all of which show up here as broken."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from core import store
    from scrapers.sources import ATS_REGISTRY, store_slug

    conn = store.connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT name, ats, slug, careers_url, wd_tenant, wd_pod, wd_site "
        "FROM companies WHERE active=1 AND ats IS NOT NULL ORDER BY name")]
    conn.close()

    # Paginating platforms would walk thousands of postings to prove one
    # endpoint is alive. The single-request ATSes are the ones worth probing
    # in bulk; the rest are covered by check_boards.py's per-platform canary.
    cheap = {"greenhouse", "lever", "ashby", "bamboohr", "jazzhr", "kula",
             "rippling", "adp", "smartrecruiters"}
    todo = [r for r in rows if r["ats"] in cheap][:limit]
    skipped = len(rows) - len(todo)

    def one(row):
        reg = ATS_REGISTRY.get(row["ats"])
        if not reg:
            return {"section": "roster", "name": row["name"], "ats": row["ats"],
                    "status": SKIPPED, "detail": "no fetcher", "seconds": 0.0}
        started = time.monotonic()
        jobs, note, exc = _run_fetcher(reg[0](row["name"], store_slug(row)))
        if jobs:
            status, detail = OK, f"{len(jobs)} postings"
        elif exc or note:
            status = verdict(f"{note} {exc}", False)
            detail = (exc or note)[:110]
        else:
            status, detail = OK, "0 postings (board alive, empty)"
        return {"section": "roster", "name": row["name"], "ats": row["ats"],
                "status": status, "detail": detail,
                "seconds": round(time.monotonic() - started, 1)}

    out = []
    real_stdout = sys.stdout
    sys.stdout = _ThreadCapture(real_stdout)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one, r): r for r in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                out.append(fut.result())
                if i % 25 == 0:
                    # This thread has no buffer, so it passes through.
                    print(f"    ...{i}/{len(todo)} boards probed")
    finally:
        sys.stdout = real_stdout
    if skipped:
        out.append({"section": "roster", "name": f"({skipped} paginating boards)",
                    "ats": "workday/sf/ultipro/...", "status": SKIPPED,
                    "detail": "not bulk-probed; see tools/check_boards.py",
                    "seconds": 0.0})
    return out


# --------------------------------------------------------------------------- #
#  Reporting                                                                   #
# --------------------------------------------------------------------------- #

SECTIONS = {
    "robots": ("robots.txt policy",
               lambda a: [probe_robots(l, u) for l, u in ROBOTS_TARGETS]),
    "feeds":  ("Aggregator feeds", lambda a: probe_feeds()),
    "search": ("Web search", lambda a: probe_search(deep=a.deep)),
    "forums": ("Forums", lambda a: probe_forums()),
    "api":    ("Keyed APIs", lambda a: probe_api()),
    "gated":  ("Gated by policy", lambda a: probe_gated()),
}


def print_section(title, results):
    print(f"\n  {title}")
    print(f"  {'-' * (len(title))}")
    for r in results:
        name = r["name"][:26]
        print(f"    {EMOJI.get(r['status'], '?')} {name:26} "
              f"{r['status']:8} {r.get('seconds', 0):5.1f}s  {r['detail'][:76]}")


def summarize(results):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


def main():
    ap = argparse.ArgumentParser(description="Comprehensive source health probe")
    ap.add_argument("--only", action="append", choices=sorted(SECTIONS),
                    help="run only these sections (repeatable)")
    ap.add_argument("--roster", action="store_true",
                    help="also probe every active board in YOUR store")
    ap.add_argument("--limit", type=int, default=None,
                    help="with --roster: cap the boards probed")
    ap.add_argument("--deep", action="store_true",
                    help="with the search section: also run the full "
                         "search->visit->parse pipeline (slow)")
    ap.add_argument("--json", type=Path, help="write machine-readable results")
    args = ap.parse_args()

    wanted = args.only or list(SECTIONS)
    # Fetchers filter internally; widen so a zero means "the source gave us
    # nothing", never "nothing matched your keywords".
    widen_keyword_filter()
    started = time.monotonic()
    all_results = []
    for key in wanted:
        title, fn = SECTIONS[key]
        try:
            res = fn(args)
        except Exception as e:
            res = [{"section": key, "name": "(section failed)", "status": BROKEN,
                    "detail": f"{type(e).__name__}: {e}"[:110], "seconds": 0.0}]
        print_section(title, res)
        all_results += res

    if args.roster:
        print("\n  Your roster (active boards)")
        print("  " + "-" * 27)
        res = probe_roster(limit=args.limit)
        bad = [r for r in res if r["status"] in (BROKEN, BLOCKED)]
        for r in sorted(bad, key=lambda r: (r["status"], r["name"])):
            print(f"    {EMOJI.get(r['status'], '?')} {r['name'][:26]:26} "
                  f"{r.get('ats', ''):14} {r['detail'][:64]}")
        alive = sum(r["status"] == OK for r in res)
        print(f"    {alive} alive, {len(bad)} failing "
              f"({sum(r['status'] == SKIPPED for r in res)} skipped)")
        all_results += res

    counts = summarize(all_results)
    print(f"\n  {'=' * 72}")
    print("  " + "   ".join(f"{EMOJI.get(k, '?')} {k}: {v}"
                            for k, v in sorted(counts.items())))
    print(f"  {len(all_results)} probes in {time.monotonic() - started:.0f}s")

    if args.json:
        args.json.write_text(json.dumps(
            {"checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
             "counts": counts, "probes": all_results}, indent=1), encoding="utf-8")
        print(f"  wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
