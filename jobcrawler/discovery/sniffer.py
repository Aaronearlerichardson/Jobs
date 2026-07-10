"""
Careers-page ATS sniffer — the single implementation shared by every
discovery path (Claude-driven discovery, BCIWiki sweeps, NC local sourcing,
ATS dorking).

Instead of guessing an ATS board slug from a company name (low recall, false
collisions), fetch the company's likely careers page(s) and detect which ATS
is embedded, extracting the *exact* slug/tenant/GUID from the embed link.

This merges the two sniffers built independently on the remote-neural and
local-clinical branches:
  * fetchable-platform coordinates + confirm-by-live-count + detection-only
    "leads" for bot-protected platforms, with concurrent URL fetching
    (remote-neural),
  * SmartRecruiters/iCIMS/SuccessFactors signatures, non-.com TLD candidate
    URLs (.xyz/.ai/.io/.bio/.health), custom-board detection, and the
    headless-browser JsSniffer for JS-rendered pages (local-clinical).

Two call styles:
  sniff_ats(name)          -> {"ats", "slug"|"triple", "careers_url"} | None
      Raw detection. Includes platforms that are only *sometimes* fetchable
      (icims, successfactors, custom) — the local track has scrapers for
      those; callers decide what to do with the coordinates.
  sniff_careers_ats(name)  -> {"confirmed": True, ats, slug, count, source_url}
                            | {"confirmed": False, ats, slug, source_url}   (lead)
                            | None
      Pipeline style: prefers a coordinate set it can CONFIRM with a live
      job count via the slug probes; anything else is surfaced as a lead.
"""

import html
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

from ..http import HEADERS
from .probes import PROBES, _extract_workday_triple, _name_domain_tokens

# ─── Platform signatures ─────────────────────────────────────────────────
#
# Fetchable platforms: regex captures the board slug; confirmable via a
# live count (slug probes / ADP requisition API). ADP needs two params
# (cid, ccId), handled specially. Workday (a triple) is detected first via
# _extract_workday_triple — highest confidence.
ATS_LINK_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("lever",      re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I)),
    ("ashby",      re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)", re.I)),
    ("kula",       re.compile(r"careers\.kula\.ai/([a-z0-9_-]+)", re.I)),
    ("jazzhr",     re.compile(r"([a-z0-9-]+)\.applytojob\.com", re.I)),
    ("bamboohr",   re.compile(r"([a-z0-9-]+)\.bamboohr\.com", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9]+)/", re.I)),
]
_ADP_CID_RE  = re.compile(r"[?&]cid=([0-9a-f-]{8,})", re.I)
_ADP_CCID_RE = re.compile(r"[?&]ccid=([0-9A-Za-z_]+)", re.I)

# Semi-fetchable: no probe/confirm path, but the local track has best-effort
# scrapers (fetchers/company.py), so sniff_ats surfaces them as coordinates
# while sniff_careers_ats treats them as leads.
SEMI_FETCHABLE_PATTERNS = [
    ("icims",           re.compile(r"([a-z0-9-]+)\.icims\.com", re.I)),
    ("successfactors",  re.compile(r"([a-z0-9-]+)\.(?:successfactors|sapsf)\.(?:com|eu)", re.I)),
]

# Detection-only platforms: real ATSes we can recognize but not reliably
# auto-fetch (bot-protected APIs or JS-only boards). Each regex captures a
# short identifying host/path for the lead note.
ATS_LEAD_PATTERNS = [
    ("eightfold",       re.compile(r"([a-z0-9-]+\.eightfold\.ai)", re.I)),
    ("dayforce",        re.compile(r"(dayforcehcm\.com/[a-zA-Z-]+/[a-zA-Z0-9_-]+)", re.I)),
    ("workable",        re.compile(r"(apply\.workable\.com/[a-z0-9-]+)", re.I)),
    ("recruitee",       re.compile(r"([a-z0-9-]+\.recruitee\.com)", re.I)),
    ("teamtailor",      re.compile(r"([a-z0-9-]+\.teamtailor\.com)", re.I)),
    ("jobvite",         re.compile(r"(jobs\.jobvite\.com/[a-z0-9-]+)", re.I)),
    ("taleo",           re.compile(r"([a-z0-9-]+\.taleo\.net)", re.I)),
    ("ukg",             re.compile(r"([a-z0-9-]+\.ultipro\.com)", re.I)),
    ("paylocity",       re.compile(r"(recruiting\.paylocity\.com/[A-Za-z0-9/_-]+)", re.I)),
    ("paycom",          re.compile(r"(paycomonline\.net/[A-Za-z0-9/_-]+)", re.I)),
    ("breezy",          re.compile(r"([a-z0-9-]+\.breezy\.hr)", re.I)),
    ("gohire",          re.compile(r"([a-z0-9-]+\.gohire\.io)", re.I)),
    # NOTE: Workday is intentionally NOT here — it's fetchable via the CXS
    # API (probe_workday confirms with a live count), so it must stay a
    # confirmable path, not a detection-only lead.
]

_BAD_SUBDOMAINS = ("www", "help", "support", "blog", "app", "careers", "jobs", "secure")

# Fetchable-ATS host detector — used to skip a provided careers_url when
# it's itself a dead slug-guess against a JSON ATS (already covered by
# slug probing upstream).
_FETCHABLE_HOST_RE = re.compile(
    r"(greenhouse\.io|lever\.co|ashbyhq\.com|kula\.ai|applytojob\.com|bamboohr\.com)",
    re.I,
)

def _looks_like_custom_board(html_text):
    """True if a page has several GENUINE job-detail links (nav/index links
    filtered out) — i.e. a self-hosted careers board worth scraping."""
    from ..fetchers.company import find_job_links
    try:
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception:
        return False
    return len(find_job_links(soup)) >= 3


# ─── Candidate careers-page URLs ─────────────────────────────────────────
#
# (tld, path) combos in priority order — breadth-first so every name token's
# high-value URLs (incl. non-.com TLDs like .xyz, common for neurotech /
# deep-tech startups) are tried before the cap.
_COMBOS = [
    ("com", "/careers"), ("com", "/careers/open-positions"),
    ("xyz", "/careers/open-positions"), ("xyz", "/careers"),
    ("ai", "/careers"), ("io", "/careers"), ("bio", "/careers"),
    ("com", "/jobs"), ("com", "/"), ("health", "/careers"), ("co", "/careers"),
    ("com", "/careers/"), ("com", "/company/careers"),
    ("com", "/join"), ("com", "/open-positions"),
]

_SNIFF_URL_CAP = 26


def _candidate_urls(name, careers_url=""):
    urls = []
    if careers_url and not _FETCHABLE_HOST_RE.search(careers_url):
        urls.append(careers_url)
    toks = _name_domain_tokens(name)
    for tld, path in _COMBOS:
        for tok in toks:
            host = f"www.{tok}.com" if tld == "com" else f"{tok}.{tld}"
            urls.append(f"https://{host}{path}")
    for tok in toks:
        urls += [f"https://careers.{tok}.com/", f"https://jobs.{tok}.com/"]
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= _SNIFF_URL_CAP:
            break
    return out


# ─── Detection ───────────────────────────────────────────────────────────

def _detect(text, final_url=""):
    """Scan text + final URL for an ATS signature.

    Returns (kind, ats, slug) where kind is "fetchable" | "semi" | "lead",
    or None. Workday first (triple, highest confidence), then ADP (two
    params, generic host), then single-capture platforms.
    """
    blob = f"{final_url}\n{text}"
    triple = _extract_workday_triple(blob)
    if triple:
        return "fetchable", "workday", triple
    if "workforcenow.adp.com" in blob.lower():
        unescaped = html.unescape(blob)
        cid = _ADP_CID_RE.search(unescaped)
        ccid = _ADP_CCID_RE.search(unescaped)
        if cid and ccid:
            return "fetchable", "adp", f"{cid.group(1)}|{ccid.group(1)}"
    for kind, patterns in (("fetchable", ATS_LINK_PATTERNS),
                           ("semi", SEMI_FETCHABLE_PATTERNS),
                           ("lead", ATS_LEAD_PATTERNS)):
        for ats, rx in patterns:
            m = rx.search(blob)
            if not m:
                continue
            slug = m.group(1)
            if kind != "lead" and slug.lower() in _BAD_SUBDOMAINS:
                continue
            if slug and len(slug) >= 2:
                return kind, ats, slug
    return None


def _confirm_coords(ats, slug):
    """Get a live job count for sniffed coordinates. Returns int or None."""
    if ats == "adp":
        cid, _, ccid = slug.partition("|")
        try:
            r = requests.get(
                "https://workforcenow.adp.com/mascsr/default/careercenter"
                "/public/events/staffing/v1/job-requisitions",
                params={"cid": cid, "ccId": ccid, "locale": "en_US", "$top": 1},
                timeout=12, headers={**HEADERS, "Accept": "application/json"},
            )
            if r.status_code != 200:
                return None
            return int(r.json().get("meta", {}).get("totalNumber", 0) or 0)
        except Exception:
            return None
    probe = PROBES.get(ats)
    if not probe:
        return None
    ok, count = probe(slug)
    return count if ok else None


def _fetch_page(url, timeout=6):
    """GET one careers-page candidate. Short timeout: most are speculative
    domain/path guesses that 404 or don't resolve; a real careers page
    answers fast. Returns the Response on 200 with real content, else None."""
    try:
        r = requests.get(url, timeout=timeout, headers=HEADERS, allow_redirects=True)
        return r if r.status_code == 200 and len(r.text) >= 300 else None
    except Exception:
        return None


def _fetch_all(urls):
    """Fetch candidates concurrently (a miss otherwise pays ~26 sequential
    GETs — the dominant per-candidate latency in a bulk run); results are
    evaluated in priority order regardless of completion order."""
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        return dict(zip(urls, pool.map(_fetch_page, urls)))


def _pack(ats, slug, careers_url):
    out = {"ats": ats, "careers_url": careers_url}
    if ats == "workday":
        out["triple"] = slug
    else:
        out["slug"] = slug
    return out


# ─── Public API ──────────────────────────────────────────────────────────

def sniff_ats(name, careers_url="", timeout=6):
    """Raw detection: first fetchable/semi-fetchable ATS found, else a
    custom self-hosted board, else None. Shape:
    {"ats", "slug"|"triple", "careers_url"}."""
    urls = _candidate_urls(name, careers_url)
    if not urls:
        return None
    responses = _fetch_all(urls)
    custom = None
    for url in urls:
        r = responses.get(url)
        if r is None:
            continue
        hit = _detect(r.text, r.url)
        if hit and hit[0] in ("fetchable", "semi"):
            return _pack(hit[1], hit[2], r.url)
        if custom is None:
            # Custom board: resolve to the page that actually holds the
            # listings (this page, or the openings page one hop away).
            from ..fetchers.company import custom_board_listing_url
            listing = custom_board_listing_url(r.url, r.text)
            if listing:
                custom = {"ats": "custom", "careers_url": listing}
    return custom


def sniff_careers_ats(name, careers_url=""):
    """Pipeline style: prefer coordinates we can CONFIRM with a live count;
    otherwise surface the highest-priority detection as a lead."""
    urls = _candidate_urls(name, careers_url)
    if not urls:
        return None
    responses = _fetch_all(urls)
    lead = None  # first (highest-priority) unconfirmable detection seen
    for url in urls:
        r = responses.get(url)
        if r is None:
            continue
        hit = _detect(r.text, r.url)
        if not hit:
            continue
        kind, ats, slug = hit
        if kind == "fetchable" and ats != "workday":
            count = _confirm_coords(ats, slug)
            if count is not None:
                return {"confirmed": True, "ats": ats, "slug": slug,
                        "count": count, "source_url": r.url}
        if lead is None:
            lead_slug = "|".join(map(str, slug)) if isinstance(slug, tuple) else slug
            lead = {"confirmed": False, "ats": ats, "slug": lead_slug,
                    "source_url": r.url}
    return lead


# ─── Headless-browser sniffer (JS-rendered careers pages) ────────────────

class JsSniffer:
    """
    Headless-browser ATS sniffer for JS-rendered careers pages (Teleflex,
    Siemens Healthineers, etc.) whose ATS link only appears after JS runs.
    Reuses one browser across calls. Degrades to no-op if Playwright is
    missing. Use as a context manager; call from a single thread.
    """

    def __init__(self):
        self._pw = self._browser = self._page = None
        self._ok = True

    def _ensure(self):
        if self._page or not self._ok:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
            from config import BROWSER_UA
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_context(
                user_agent=BROWSER_UA, viewport={"width": 1440, "height": 900},
                locale="en-US").new_page()
        except Exception as e:
            print(f"    [js-sniff] disabled ({e})")
            self._ok = False
        return self._page

    def sniff(self, name, careers_url=""):
        page = self._ensure()
        if not page:
            return None
        for url in _candidate_urls(name, careers_url):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                continue
            for _ in range(2):
                try:
                    hit = _detect(page.content(), page.url)
                except Exception:
                    hit = None
                if hit and hit[0] in ("fetchable", "semi"):
                    return _pack(hit[1], hit[2], page.url)
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    break
        return None

    def close(self):
        for obj in (self._browser, self._pw):
            try:
                obj and (obj.close() if obj is self._browser else obj.stop())
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# Back-compat: modules that imported the signature table from here.
_SIGS = ATS_LINK_PATTERNS + SEMI_FETCHABLE_PATTERNS
