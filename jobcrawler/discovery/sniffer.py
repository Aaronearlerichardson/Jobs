"""
Careers-page ATS sniffer.

Instead of guessing an ATS board slug from a company name (low recall, false
collisions), fetch the company's likely careers page(s) and detect which ATS is
embedded, extracting the *exact* slug/tenant. Covers Greenhouse, Lever, Ashby,
Workday, SmartRecruiters, iCIMS, and SuccessFactors.

Returns a dict: {"ats", "slug"|"triple", "careers_url"} or None.
"""

import re

import requests
from bs4 import BeautifulSoup

from ..http import HEADERS
from .probes import _extract_workday_triple, _name_domain_tokens

def _looks_like_custom_board(html):
    """True if a page has several GENUINE job-detail links (nav/index links
    filtered out) — i.e. a self-hosted careers board worth scraping."""
    from ..local_fetch import find_job_links
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return False
    return len(find_job_links(soup)) >= 3

# ATS URL signatures. Each maps to a capture of the slug/tenant.
_SIGS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?for=([a-z0-9]+)", re.I)),
    ("lever",      re.compile(r"jobs\.lever\.co/([a-z0-9\-]+)", re.I)),
    ("ashby",      re.compile(r"jobs\.ashbyhq\.com/([a-z0-9\-]+)", re.I)),
    ("smartrecruiters", re.compile(r"careers\.smartrecruiters\.com/([A-Za-z0-9]+)", re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9]+)/", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9]+)", re.I)),
    ("icims",      re.compile(r"([a-z0-9\-]+)\.icims\.com", re.I)),
    ("successfactors", re.compile(r"([a-z0-9\-]+)\.(?:successfactors|sapsf)\.(?:com|eu)", re.I)),
]

_CAREERS_PATHS = ("/careers", "/careers/", "/careers/open-positions", "/en/careers",
                  "/company/careers", "/jobs", "/about/careers", "")
# Not just .com — startups (esp. neurotech/deep-tech) use .xyz/.ai/.io/.bio/.health.
_TLDS = ("com", "xyz", "ai", "io", "bio", "health", "co")


# (tld, path) combos in priority order — breadth-first so every name token's
# high-value URLs (incl. non-.com TLDs like .xyz) are tried before the cap.
_COMBOS = [
    ("com", "/careers"), ("com", "/careers/open-positions"),
    ("xyz", "/careers/open-positions"), ("xyz", "/careers"),
    ("ai", "/careers"), ("io", "/careers"), ("bio", "/careers"),
    ("com", "/jobs"), ("com", "/"), ("health", "/careers"), ("co", "/careers"),
    ("com", "/careers/"), ("com", "/company/careers"),
]


def _candidate_urls(name, careers_url=""):
    urls = [careers_url] if careers_url else []
    toks = _name_domain_tokens(name)
    for tld, path in _COMBOS:
        for tok in toks:
            host = f"www.{tok}.com" if tld == "com" else f"{tok}.{tld}"
            urls.append(f"https://{host}{path}")
    for tok in toks:
        urls += [f"https://careers.{tok}.com/", f"https://jobs.{tok}.com/"]
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:26]


def _detect(text, final_url):
    """Scan text + final URL for an ATS signature; return (ats, slug) or None."""
    blob = f"{final_url}\n{text}"
    # Workday first (triple, highest confidence).
    triple = _extract_workday_triple(blob)
    if triple:
        return "workday", triple
    for ats, rx in _SIGS:
        m = rx.search(blob)
        if not m:
            continue
        slug = m.group(1)
        # Guard against generic/framework captures.
        if ats == "icims" and slug.lower() in ("www", "careers", "jobs", "secure"):
            continue
        if slug and len(slug) >= 2:
            return ats, slug
    return None


def _pack(ats, slug, careers_url):
    out = {"ats": ats, "careers_url": careers_url}
    if ats == "workday":
        out["triple"] = slug
    else:
        out["slug"] = slug
    return out


def sniff_ats(name, careers_url="", timeout=10):
    """Fetch candidate careers pages (static) and detect the embedded ATS + slug."""
    for url in _candidate_urls(name, careers_url):
        try:
            r = requests.get(url, timeout=timeout, headers=HEADERS, allow_redirects=True)
        except Exception:
            continue
        if r.status_code != 200 or len(r.text) < 300:
            continue
        hit = _detect(r.text, r.url)
        if hit:
            return _pack(hit[0], hit[1], r.url)
        # Custom board: resolve to the page that actually holds the listings
        # (this page, or the openings page it links to one hop away).
        from ..local_fetch import custom_board_listing_url
        listing = custom_board_listing_url(r.url, r.text)
        if listing:
            return {"ats": "custom", "careers_url": listing}
    return None


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
                if hit:
                    return _pack(hit[0], hit[1], page.url)
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
