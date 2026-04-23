"""HTML-scrape fetchers: Kula, SuccessFactors, ad-hoc custom pages."""

import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..filters import is_relevant
from ..http import HEADERS


def fetch_kula(company_name, kula_slug):
    base_url = f"https://careers.kula.ai/{kula_slug}"
    try:
        r = requests.get(base_url, timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Kula {company_name}: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    apply_links = soup.find_all("a", href=re.compile(rf"/{re.escape(kula_slug)}/\d+"))
    jobs = []
    for a in apply_links:
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
        dept  = lines[0] if len(lines) > 1 else ""
        loc   = lines[2] if len(lines) > 2 else "See posting"

        if is_relevant(f"{title} {dept}"):
            jobs.append({
                "id": f"kula_{kula_slug}_{jid.group(1)}",
                "company": company_name,
                "title": title,
                "url": href,
                "location": loc.split(";")[0].strip(),
                "description": "",
            })
    return jobs


def fetch_custom(company_name, page_url, css_selector=None):
    try:
        r = requests.get(page_url, timeout=20, headers=HEADERS)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Custom {company_name}: {e}")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    anchors = soup.select(css_selector) if css_selector else [
        a for a in soup.find_all("a", href=True)
        if is_relevant(a.get_text(strip=True))
    ]
    jobs, seen = [], set()
    for a in anchors:
        title = a.get_text(strip=True)
        href  = a.get("href", "")
        if not href.startswith("http"):
            href = urljoin(page_url, href)
        if len(title) < 5 or href in seen or not is_relevant(title):
            continue
        seen.add(href)
        jobs.append({
            "id": f"custom_{company_name.replace(' ','_')}_{abs(hash(href))}",
            "company": company_name, "title": title,
            "url": href, "location": "See posting", "description": "",
        })
    return jobs


def fetch_successfactors(company_name, base_url, step=25, max_pages=80):
    """
    Scrape a SuccessFactors career site (e.g. careers.duke.edu). SF serves
    ~25 jobs per HTML page at /search/?startrow=N. Each tile has two anchors
    (image + title) so we dedupe by URL. Stop when a page adds zero new URLs.
    """
    jobs, seen = [], set()
    sf_headers = {**HEADERS, "Accept": "text/html"}
    for page in range(max_pages):
        startrow = page * step
        url = f"{base_url.rstrip('/')}/search/?startrow={startrow}"
        try:
            r = requests.get(url, timeout=25, headers=sf_headers)
            r.raise_for_status()
        except Exception as e:
            print(f"    [!] SuccessFactors {company_name} p{page}: {e}")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        anchors = soup.select("a.jobTitle-link") or [
            a for a in soup.find_all("a", href=True) if "/job/" in a["href"]
        ]
        if not anchors:
            break

        new_on_page = 0
        for a in anchors:
            href = a.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = urljoin(base_url, href)
            if href in seen:
                continue
            seen.add(href)

            title = a.get_text(strip=True)
            loc = "See posting"
            row = a.find_parent("tr") or a.find_parent("li") or a.find_parent("div")
            if row is not None:
                text = row.get_text(" ", strip=True)
                m = re.search(
                    r"(Durham|Chapel Hill|Raleigh|Research Triangle|RTP|"
                    r"Carrboro|Cary|Morrisville|Charlotte|Greensboro|"
                    r"Winston[- ]Salem|Asheville|North Carolina|NC|"
                    r"Remote|Virginia|VA|Richmond)[^|\n]{0,40}",
                    text, flags=re.I,
                )
                if m:
                    loc = m.group(0).strip(" ,-")

            jid_m = re.search(r"/job/([^/?#]+)", href)
            jid = jid_m.group(1) if jid_m else str(abs(hash(href)))
            new_on_page += 1
            if is_relevant(title):
                jobs.append({
                    "id":          f"sf_{company_name.replace(' ','_')}_{jid}",
                    "company":     company_name,
                    "title":       title,
                    "url":         href,
                    "location":    loc,
                    "description": "",
                })

        if new_on_page == 0:
            break
        time.sleep(0.3)

    return jobs
