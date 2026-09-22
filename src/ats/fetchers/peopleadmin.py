"""PeopleAdmin Atom-feed scraper.

A tenant publishes two Atom feeds and they are not interchangeable:

    /postings/all_jobs.atom   every open posting
    /postings/search.atom     whatever the tenant's default saved search returns

So `all_jobs` is asked for first and `search` is only the fallback.

An entry carries <title>, <link href>, <published>/<updated>, <content> (the
posting body, as escaped HTML) and <author><name> (the hiring department,
e.g. "Epidemiology - 463501"). There is no location element and no
<category>: a tenant is one campus. A posting's location is the place its
title names, else the campus the feed's own <title> names.

Notes:
    Both feeds live under a robots.txt that blanket-disallows `*` — written
    for the HTML site, applied to the machine-facing feed by accident. The
    crawler will not fetch a tenant until its host is listed in the
    profile's [policy] robots_exempt_hosts, which is the operator's call to
    make per host, not this module's. See profile.example.toml.

    A posting's detail PAGE does name its place, in a "Position Location"
    or "Job Location" table row (two tenants live-probed 2026-09-17).
    It is not read: that is one HTML GET per posting, on the part of the
    host the robots.txt disallow was written for. The body is not read
    for a place either: on 2026-09-17 a third of one tenant's stored
    locations were prose fragments cut from it ("<State> at <City> is
    seeking two tenure-tr", "... Driver's License", "remote work
    location, consistent with ...").
"""

import re

from bs4 import BeautifulSoup

from src.match.locality import location_snippet
from src.net.http import HEADERS, SESSION, fetch_failed
from src.net.util import (clean_field, norm_posted_date, stable_id,
                          text_from_html)

# Tried in order; the first feed with entries wins.
FEED_PATHS = ("/postings/all_jobs.atom", "/postings/search.atom")

_HOST_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?([^/?#]+)", re.I)
_POSTING_ID_RE = re.compile(r"/postings/(\d+)")


def feed_host(value):
    """The tenant hostname in `value`, lowercased.

    Store rows for this ATS are keyed on `careers_url`, so callers hand in
    anything from a bare host to a full feed URL:

    >>> feed_host("unc.peopleadmin.com")
    'unc.peopleadmin.com'
    >>> feed_host("https://jobs.ncsu.edu/postings/all_jobs.atom")
    'jobs.ncsu.edu'

    Input with no host of its own yields the empty string:

    >>> feed_host("/postings/all_jobs.atom"), feed_host(""), feed_host(None)
    ('', '', '')
    """
    m = _HOST_RE.match((value or "").strip())
    return m.group(1).lower() if m else ""


def tenant_key(host):
    """`host` slugified into the job-id namespace for that tenant.

    >>> tenant_key("unc.peopleadmin.com")
    'unc_peopleadmin_com'

    The whole host is used, not its first label, because the tenants that
    serve PeopleAdmin from their own domain share the first one:

    >>> tenant_key("jobs.ncsu.edu"), tenant_key("jobs.uncw.edu")
    ('jobs_ncsu_edu', 'jobs_uncw_edu')
    """
    return re.sub(r"[^a-z0-9]+", "_", (host or "").lower()).strip("_")


def _text(el):
    return el.get_text(" ", strip=True) if el is not None else ""


def _parse_feed(xml, host, company_name, gate=None):
    """(entries seen, relevant postings) from one Atom document.

    The entry count is separate from the postings because they answer
    different questions: an empty feed is a reason to try the next
    FEED_PATH, a feed whose entries all failed the relevance gate is not.
    """
    soup = BeautifulSoup(xml, "xml")
    # The feed's own <title> names the institution ("<Campus>: All Jobs"),
    # and a tenant is one campus, so it stands in for entries whose title
    # names no place, which is most of them.
    campus = clean_field(location_snippet(_text(soup.find("title")), ""))
    key = tenant_key(host)
    entries = soup.find_all("entry")
    jobs = []
    for e in entries:
        # get_text(strip=True) trims each XML text node's ends but not an
        # embedded newline/tab INSIDE one -- a feed that wraps its own
        # <title> onto two lines carries it straight through; clean_field
        # is the same hygiene board.board_jobs applies to every row that
        # goes through it, which these rows never do.
        title = clean_field(_text(e.find("title")))
        link_el = e.find("link")
        jurl = (link_el.get("href") if link_el and link_el.has_attr("href")
                else _text(e.find("id")))          # the entry id IS the URL
        if not jurl or not title:
            continue
        # <content> on current tenants, <summary> on older ones. Either way
        # the body is escaped HTML: unescaped once by the XML parser, then
        # stripped of its tags.
        body = text_from_html(_text(e.find("content") or e.find("summary")))
        # <author><name> is the hiring department. Folding it into the
        # description is what puts "Epidemiology" or "Neurology" in front of
        # the keyword and title gates, which see no other structured field.
        author = e.find("author")
        dept = _text(author.find("name")) if author is not None else ""
        desc = " | ".join(p for p in (dept, body) if p)
        if gate is not None and not gate(title, desc):
            continue
        jid_m = _POSTING_ID_RE.search(jurl)
        jobs.append({
            "id":          f"pa_{key}_{jid_m.group(1) if jid_m else stable_id(jurl)}",
            "company":     company_name,
            "title":       title,
            "url":         jurl,
            # "" when neither the title nor the campus names a place --
            # NOT a "See posting" placeholder, which location-scoped
            # callers cannot tell from a real location. `title` is already
            # clean_field'd above, so a snippet cut from it needs no
            # second pass; `campus` (the fallback) was cleaned once, up
            # front, for the same reason.
            "location":    location_snippet(title, "") or campus,
            "description": desc,
            "posted_at":   norm_posted_date(_text(e.find("published"))
                                            or _text(e.find("updated"))),
        })
    return len(entries), jobs


def fetch_peopleadmin(host, company_name, gate=None):
    """Relevant postings from a PeopleAdmin tenant's Atom feed.

    `host` may be a bare hostname or any URL on the tenant. FEED_PATHS are
    tried in order and the first feed with entries wins; one that errors or
    comes back empty falls through to the next. A fetch that fails is
    reported and returns [] rather than raising.

    See tests/test_fetcher_parsers.py::TestPeopleAdmin.
    """
    host = feed_host(host)
    if not host:
        return []
    label = company_name or host
    for path in FEED_PATHS:
        try:
            r = SESSION.get(f"https://{host}{path}",
                            headers={**HEADERS, "Accept": "application/atom+xml"})
            r.raise_for_status()
        except Exception as e:
            fetch_failed(f"PeopleAdmin {label} {path}", e)
            continue
        entries, jobs = _parse_feed(r.text, host, company_name, gate)
        if entries:
            return jobs
        print(f"    [!] PeopleAdmin {label}: {path} returned no entries")
    return []
