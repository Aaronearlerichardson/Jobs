"""HTML-scrape fetchers: Kula and SuccessFactors career sites.

Neither exposes a JSON listing, so the rows are read off the server-rendered
pages; neither carries a description in its listing, and neither has a
detail call here (descriptions are hydrated later from the job URL). Both
hand their rows to ``board.board_jobs`` for the location and relevance
filters.
"""

import re
import time
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

from src.match.locality import location_snippet
from src.net.http import HEADERS, SESSION, fetch_failed
from src.net.util import stable_id
from .board import board_jobs


def _kula_rows(kula_slug, soup):
    for a in soup.find_all("a", href=re.compile(rf"/{re.escape(kula_slug)}/\d+")):
        href = a["href"]
        if not href.startswith("http"):
            href = urljoin("https://careers.kula.ai", href)
        jid = re.search(r"/(\d+)/?$", href)
        if not jid:
            continue

        parent = a.parent
        lines = []
        for _ in range(8):
            raw = parent.get_text("\n").strip()
            lines = [l.strip() for l in raw.split("\n") if len(l.strip()) > 3]
            if len(lines) >= 2:
                break
            parent = parent.parent

        title = lines[1] if len(lines) > 1 else lines[0] if lines else "Unknown"
        dept = lines[0] if len(lines) > 1 else ""
        loc = lines[2] if len(lines) > 2 else "See posting"
        yield {"id": f"kula_{kula_slug}_{jid.group(1)}", "title": title,
               "url": href, "location": loc.split(";")[0].strip(),
               "description": "", "head": f"{title} {dept}"}


def fetch_kula(company_name, kula_slug, gate=None, loc_re=None):
    base_url = f"https://careers.kula.ai/{kula_slug}"
    try:
        r = SESSION.get(base_url, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        return fetch_failed(f"Kula {company_name or kula_slug}", e)
    soup = BeautifulSoup(r.text, "html.parser")
    return board_jobs(_kula_rows(kula_slug, soup), company_name,
                      gate=gate, loc_re=loc_re)


# SF encodes "<City>,-<ST>" at the head of the /job/ slug, spaces as hyphens
# (e.g. "/job/Holly-Springs,-NC-<title>-NC-27540/"). Non-greedy up to the
# first ",-<2 caps>-" so a comma inside the title can't steal the match.
_SF_LOC_SLUG_RE = re.compile(r"/job/(.+?),-([A-Z]{2})-")


def _sf_rows(base_url, label, step, max_pages):
    """Every posting on a SuccessFactors site, paged; a page that adds no
    new URL ends the walk (the last page repeats on some tenants)."""
    seen = set()
    sf_headers = {**HEADERS, "Accept": "text/html"}
    key = re.sub(r"[^a-z0-9]+", "", base_url.lower())[-16:]
    for page in range(max_pages):
        url = f"{base_url.rstrip('/')}/search/?startrow={page * step}"
        try:
            r = SESSION.get(url, headers=sf_headers)
            r.raise_for_status()
        except Exception as e:
            fetch_failed(f"SuccessFactors {label} p{page}", e)
            return
        soup = BeautifulSoup(r.text, "html.parser")
        anchors = soup.select("a.jobTitle-link") or [
            a for a in soup.find_all("a", href=True) if "/job/" in a["href"]]
        if not anchors:
            return
        new_on_page = 0
        for a in anchors:
            href: str = a.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = urljoin(base_url, href)
            if href in seen:
                continue
            seen.add(href)
            new_on_page += 1
            # Location: the /job/ slug names "City,-ST" (boards like OXB's
            # carry no location text in the row); else the row text, else
            # the placeholder.
            m = _SF_LOC_SLUG_RE.search(unquote(href))
            if m:
                loc = f"{m.group(1).replace('-', ' ').strip()}, {m.group(2)}"
            else:
                row = a.find_parent("tr") or a.find_parent("li") or a.find_parent("div")
                loc = (location_snippet(row.get_text(" ", strip=True))
                       if row is not None else "See posting")
            # Numeric requisition id first, slug fallback — and the id token
            # comes from the BOARD URL, not the (sometimes empty) display
            # name, so the profile feed and the company store ingest one
            # row per posting (Duke postings were double-ranked and double-
            # deep-verified when two schemes disagreed, 2026-08-28).
            jid_m = (re.search(r"/job/[^/]+/(\d+)", href)
                     or re.search(r"/job/([^/?#]+)", href))
            jid = jid_m.group(1) if jid_m else stable_id(href)
            yield {"id": f"sf_{key}_{jid}", "title": a.get_text(strip=True),
                   "url": href, "location": loc, "description": ""}
        if new_on_page == 0:
            return
        time.sleep(0.3)


def fetch_successfactors(company_name, base_url, gate=None, loc_re=None, step=25,
                         max_pages=80):
    """Scrape a SuccessFactors career site (e.g. careers.duke.edu). SF serves
    ~25 jobs per HTML page at /search/?startrow=N. Each tile has two anchors
    (image + title) so rows dedupe by URL."""
    return board_jobs(_sf_rows(base_url, company_name or base_url, step, max_pages),
                      company_name, gate=gate, loc_re=loc_re)
