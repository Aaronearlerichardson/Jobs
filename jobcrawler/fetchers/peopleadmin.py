"""PeopleAdmin Atom-feed scraper."""

import re

import requests
from bs4 import BeautifulSoup

from ..filters import is_relevant
from ..http import HEADERS
from ..util import stable_id


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
    # The feed's own <title> names the institution ("UNC-Chapel Hill ...") —
    # a PeopleAdmin host is one campus, so it doubles as the location for
    # entries whose text never names a city (most of them). Without this,
    # every unlocated entry read "See posting" and location-scoped callers
    # dropped the entire board.
    feed_title_el = soup.find("title")
    feed_loc = feed_title_el.get_text(strip=True) if feed_title_el else ""
    entries = soup.find_all("entry")
    jobs = []
    _CITY_RE = re.compile(
        r"(Chapel Hill|Durham|Raleigh|Carrboro|Research Triangle|RTP|"
        r"Charlotte|Greensboro|Winston[- ]Salem|Asheville|"
        r"North Carolina|NC|Remote)[^|\n]{0,40}", re.I)
    for e in entries:
        title_el = e.find("title")
        title    = title_el.get_text(strip=True) if title_el else ""
        link_el  = e.find("link")
        jurl     = link_el.get("href") if link_el and link_el.has_attr("href") else ""
        if not jurl or not title:
            continue
        # PeopleAdmin feeds carry the posting text in <summary> on some
        # tenants and <content> on others (UNC).
        body_el = e.find("summary") or e.find("content")
        desc    = body_el.get_text(" ", strip=True) if body_el else ""

        loc = "See posting"
        m = _CITY_RE.search(desc)
        if m:
            loc = m.group(0).strip(" ,-")
        elif feed_loc:
            fm = _CITY_RE.search(feed_loc)
            loc = fm.group(0).strip(" ,-") if fm else feed_loc[:60]

        jid_m = re.search(r"/postings/(\d+)", jurl)
        jid   = jid_m.group(1) if jid_m else stable_id(jurl)
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
