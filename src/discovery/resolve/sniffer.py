"""
Careers-page ATS sniffer — the single implementation shared by every
discovery path (Claude-driven discovery, BCIWiki sweeps, local sourcing,
ATS dorking).

Instead of guessing an ATS board slug from a company name (low recall, false
collisions), fetch the company's likely careers page(s) and detect which ATS
is embedded, extracting the *exact* slug/tenant/GUID from the embed link.

The pieces it is built from live one layer down and are shared with the
Workday probes: the signature tables and `detect`/`pack` in
src.ats.signatures, and the candidate-URL generation, per-run fetch memo
and identity guards in this package.
"""

import logging

from bs4 import BeautifulSoup, SoupStrainer

from src.config import PROBE_TIMEOUT
from src.ats.signatures import detect, pack
from src.net.http import HEADERS, SESSION
from .fetchpool import ROOT_PATTERNS, candidate_urls
from .identity import (_foreign_board, candidate_pages,
                       candidate_responses, corroborated)
from .probes import PROBES

# File-only diagnostics (session log DEBUG channel — never printed).
_log = logging.getLogger("src.discovery.resolve.sniffer")

_ANCHORS_ONLY = SoupStrainer("a")


def _looks_like_custom_board(html_text):
    """True if a page has several GENUINE job-detail links (nav/index links
    filtered out) — i.e. a self-hosted careers board worth scraping."""
    from src.ats.fetchers.company import find_job_links
    try:
        soup = BeautifulSoup(html_text, "lxml", parse_only=_ANCHORS_ONLY)
    except Exception:
        return False
    return len(find_job_links(soup)) >= 3


def _scan_root(name, careers_url=""):
    """Fetch the bare homepage(s) (candidate_urls with ROOT_PATTERNS) and
    return the first fetchable/semi-fetchable ATS hit (packed like
    sniff_ats), else None.

    The root is a regular candidate too, but a multi-token name or a
    careers_url hint can push it past the cap, so the failure path scans it
    separately (the per-run memo makes an already-fetched root free). A hit
    reached only through a risky (truncated/generic) domain token must still
    corroborate the company name on the page -- the same rule candidate hits
    are held to -- so scanning the root doesn't hand the galaxy.com
    collision a second way in (network path, so covered by
    tests/test_parsers.py::TestRootScan rather than a doctest here).
    """
    for r in candidate_pages(name, careers_url, patterns=ROOT_PATTERNS,
                             cap=None):
        hit = detect(r.text, r.url)
        if hit and hit[0] in ("fetchable", "semi"):
            if hit[1] == "workday" and _foreign_board(name, hit[2]):
                continue
            return pack(hit[1], hit[2], r.url)
    return None


def _confirm_coords(ats, slug):
    """Get a live job count for sniffed coordinates. Returns int or None."""
    if ats == "adp":
        cid, _, ccid = slug.partition("|")
        try:
            r = SESSION.get(
                "https://workforcenow.adp.com/mascsr/default/careercenter"
                "/public/events/staffing/v1/job-requisitions",
                params={"cid": cid, "ccId": ccid, "locale": "en_US", "$top": 1},
                timeout=PROBE_TIMEOUT,
                headers={**HEADERS, "Accept": "application/json"},
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


# ─── Public API ──────────────────────────────────────────────────────────

def sniff_ats(name, careers_url=""):
    """Raw detection: first fetchable/semi-fetchable ATS found, else a
    custom self-hosted board, else None. Shape:
    {"ats", "slug"|"triple", "careers_url"}."""
    custom = None
    n_pages = 0
    for r in candidate_pages(name, careers_url):
        n_pages += 1
        hit = detect(r.text, r.url)
        if hit and hit[0] in ("fetchable", "semi"):
            if hit[1] == "workday" and _foreign_board(name, hit[2]):
                hit = None      # keep scanning; the custom fallback may
            else:               # still capture the company's OWN listings
                _log.debug("sniff %s: %s %r found on %s",
                           name, hit[1], hit[2], r.url)
                return pack(hit[1], hit[2], r.url)
        if custom is None:
            # Custom board: resolve to the page that actually holds the
            # listings (this page, or the openings page one hop away).
            from src.ats.fetchers.company import custom_board_listing_url
            listing = custom_board_listing_url(r.url, r.text)
            if listing:
                custom = {"ats": "custom", "careers_url": listing}
    # Counted after the walk rather than before it: "3 answered" was the
    # old line, and a page that answered but failed the identity check is
    # not a page this sniff could read anything off.
    _log.debug("sniff %s: %d usable candidate page(s), no ATS", name, n_pages)
    # Every careers-path candidate missed: a company whose ATS badge sits on
    # the homepage itself (no dedicated /careers page -- see _scan_root)
    # still has one more place to look before this is a miss.
    root_hit = _scan_root(name, careers_url)
    if root_hit:
        return root_hit
    return custom


def sniff_careers_ats(name, careers_url=""):
    """Pipeline style: prefer coordinates we can CONFIRM with a live count;
    otherwise surface the highest-priority detection as a lead."""
    lead = None  # first (highest-priority) unconfirmable detection seen
    for r in candidate_pages(name, careers_url):
        hit = detect(r.text, r.url)
        if not hit:
            continue
        kind, ats, slug = hit
        if ats == "workday" and _foreign_board(name, slug):
            continue
        if kind == "fetchable" and ats != "workday":
            count = _confirm_coords(ats, slug)
            if count is not None:
                return {"confirmed": True, "ats": ats, "slug": slug,
                        "count": count, "source_url": r.url}
        if lead is None:
            lead_slug = "|".join(map(str, slug)) if isinstance(slug, tuple) else slug
            lead = {"confirmed": False, "ats": ats, "slug": lead_slug,
                    "source_url": r.url}
    if lead:
        return lead
    # No candidate careers-path yielded even an unconfirmable lead -- try the
    # bare homepage (see sniff_ats's matching fallback / _scan_root).
    root_hit = _scan_root(name, careers_url)
    if root_hit:
        ats, slug = root_hit["ats"], root_hit.get("slug", root_hit.get("triple"))
        count = _confirm_coords(ats, slug) if ats != "workday" else None
        if count is not None:
            return {"confirmed": True, "ats": ats, "slug": slug,
                    "count": count, "source_url": root_hit["careers_url"]}
        slug_str = "|".join(map(str, slug)) if isinstance(slug, tuple) else slug
        return {"confirmed": False, "ats": ats, "slug": slug_str,
                "source_url": root_hit["careers_url"]}
    return None


# ─── "no-board-found" subcategories ───────────────────────────────────────
#
# A bare "no-board-found" means "we don't know why" -- which of the very
# different failure modes below it was is invisible until someone probes by
# hand. diagnose_no_board turns the sniff's own fetch results into one of
# four qualifiers (board.classify_miss appends it to the
# "no-board-found" family, e.g. "no-board-found:site-only-no-careers").

def diagnose_no_board(name, careers_url=""):
    """Why sniff_careers_ats found nothing for `name`, one of:

    - "domain-unreachable": not one candidate URL answered at all (DNS/SSL/
      timeout on every guess) -- likely defunct or acquired.
    - "wrong-domain": every page that DID answer was reached only through a
      truncated/generic domain token (see names.risky_domain_tokens) and
      none corroborated the company name -- the precise domain never
      answered, so all we have is someone else's page (the galaxy.com
      shape: "galaxydiagnostics.com" dead, "galaxy.com" live).
    - "careers-page-no-ats": at least one legitimately-reached page (a safe
      token, or a risky one that DID corroborate) looks like a real
      self-hosted job board (>=3 genuine job-detail links), but no
      recognized ATS is embedded on it.
    - "site-only-no-careers": at least one legitimately-reached page
      answered, but none of them is a careers page or has a detectable
      ATS -- the domain resolves, nothing else does.

    A risky-token hit is judged only when nothing safer answered: if the
    real domain (or any corroborating page) responds too, a coincidental
    unrelated site at a truncated-token guess is just noise, not evidence
    this company's domain is wrong.

    A name with no domain tokens to guess and no careers_url hint has no
    candidate URL to even attempt:

    >>> diagnose_no_board("")
    'domain-unreachable'

    The other three qualifiers all need a live fetch to demonstrate (a real
    candidate response, not just an empty candidate list), so they are
    covered by tests/test_parsers.py::TestDiagnoseNoBoard instead of a
    doctest here.

    Notes:
        Costs its own fetch pass (re-derives the candidate list rather than
        reusing sniff_careers_ats's), so it is called only on the already-
        established failure path in classify_miss, never per candidate in
        a bulk pass.
    """
    # The RAW pass (identity.candidate_responses), not the filtered walk:
    # this function exists to tell "nothing answered" from "everything that
    # answered was somebody else", and candidate_pages has already dropped
    # the evidence for that distinction.
    answered = (candidate_responses(name, careers_url)
                + candidate_responses(name, careers_url,
                                      patterns=ROOT_PATTERNS, cap=None))
    hits = [(u, r) for u, r in answered if r is not None]
    if not hits:
        return "domain-unreachable"
    safe_hits, saw_risky_uncorroborated = [], False
    for url, r in hits:
        if not corroborated(url, name, r.text):
            saw_risky_uncorroborated = True
            continue
        safe_hits.append(r)
    if not safe_hits:
        return "wrong-domain" if saw_risky_uncorroborated else "domain-unreachable"
    if any(_looks_like_custom_board(r.text) for r in safe_hits):
        return "careers-page-no-ats"
    return "site-only-no-careers"


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
            from src.config import BROWSER_UA
            from .probes import launch_chromium
            self._pw = sync_playwright().start()
            self._browser, _ = launch_chromium(self._pw, headless=True)
            self._page = self._browser.new_context(
                user_agent=BROWSER_UA, viewport={"width": 1440, "height": 900},
                locale="en-US").new_page()
        except Exception as e:
            # Same one-shot reporting as the Workday JS probe — a missing
            # browser is one condition, not one per instance.
            from .probes import _js_launch_hint, _report_js_disabled
            _report_js_disabled(f"careers-page sniff: {_js_launch_hint(e)}")
            self._ok = False
        return self._page

    def sniff(self, name, careers_url=""):
        page = self._ensure()
        if not page:
            return None
        for url in candidate_urls(name, careers_url):
            # Fetched by the browser, not the pool, so this one cannot use
            # candidate_pages -- but the identity check is the same rule.
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                continue
            for _ in range(2):
                try:
                    content = page.content()
                    hit = detect(content, page.url)
                except Exception:
                    content, hit = "", None
                if hit and hit[0] in ("fetchable", "semi"):
                    if not corroborated(url, name, content):
                        break
                    return pack(hit[1], hit[2], page.url)
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
