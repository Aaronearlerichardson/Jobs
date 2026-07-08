"""PeopleAdmin Atom-feed scraper."""

import re

import requests
from bs4 import BeautifulSoup

from ..filters import is_relevant
from ..http import HEADERS


def fetch_peopleadmin(host, company_name):
    """
    Scrape a PeopleAdmin career site via its Atom feed:
      https://{host}/postings/search.atom
    """
    url = f"https://{host}/postings/search.atom"
    try:
        r = requests.get(url, timeout=25,
                         headers={**HEADERS, "Accept": "application/atom+xml"})
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] PeopleAdmin {company_name}: {e}")
        return []

    soup = BeautifulSoup(r.text, "xml")
    entries = soup.find_all("entry")
    jobs = []
    for e in entries:
        title_el = e.find("title")
        title    = title_el.get_text(strip=True) if title_el else ""
        link_el  = e.find("link")
        jurl     = link_el.get("href") if link_el and link_el.has_attr("href") else ""
        if not jurl or not title:
            continue
        summary  = e.find("summary")
        desc_raw = summary.get_text(" ", strip=True) if summary else ""
        desc     = desc_raw[:600]

        loc = "See posting"
        m = re.search(
            r"(Chapel Hill|Durham|Raleigh|Carrboro|Research Triangle|RTP|"
            r"Charlotte|Greensboro|Winston[- ]Salem|Asheville|"
            r"North Carolina|NC|Remote)[^|\n]{0,40}",
            desc_raw, flags=re.I,
        )
        if m:
            loc = m.group(0).strip(" ,-")

        jid_m = re.search(r"/postings/(\d+)", jurl)
        jid   = jid_m.group(1) if jid_m else str(abs(hash(jurl)))
        if is_relevant(title, desc):
            jobs.append({
                "id":          f"pa_{host.split('.')[0]}_{jid}",
                "company":     company_name,
                "title":       title,
                "url":         jurl,
                "location":    loc,
                "description": desc,
            })
    return jobs
