"""Extract jobs + companies from manually captured page HTML.

The supply side of the manual capture flow: you browse LinkedIn / Indeed /
any job board logged in as yourself, and either click the userscript button
(POSTs the live DOM to capture.py's local server) or save the page with
Ctrl+S and run `python capture.py <files>`. This module turns that HTML
into normalized job dicts:

    {id, title, company, url, location, description}

Parsing is layered: site-specific selectors for LinkedIn and Indeed cards,
then JSON-LD JobPosting blocks, then a generic job-link sweep — whichever
layers hit, results are merged and de-duplicated by job id.
"""

import json
import re

from bs4 import BeautifulSoup

from .util import stable_id

_LI_VIEW_RE = re.compile(r"/jobs/view/(\d+)")
_LI_CURRENT_RE = re.compile(r"currentJobId=(\d+)")
_INDEED_JK_RE = re.compile(r"[?&]jk=([0-9a-f]+)", re.I)


def _txt(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def _sel(scope, *selectors):
    for sel in selectors:
        el = scope.select_one(sel)
        if el and _txt(el):
            return _txt(el)
    return ""


# Aggregator / ATS / social hosts — a URL on one of these is NOT the
# company's own website, so it can't seed a careers-page guess for the lead
# resolver. Only a company-owned domain is worth recording.
_AGG_HOST_RE = re.compile(
    r"linkedin\.|indeed\.|glassdoor\.|ziprecruiter\.|simplyhired|monster\.|"
    r"dice\.|greenhouse\.|lever\.co|ashbyhq|myworkdayjobs|smartrecruiters|"
    r"icims|bamboohr|jazzhr|applytojob|paylocity|paycom|workable|breezy|"
    r"google\.com|facebook\.|twitter\.|x\.com|youtube\.|instagram\.|"
    r"crunchbase|builtin|wellfound|schema\.org", re.I)


def _company_site(*urls):
    """First real company-owned website (scheme+host) among the given URLs,
    skipping aggregator/ATS/social hosts. Recorded on a lead as careers_url so
    the resolver can probe {domain}/careers instead of guessing the domain from
    the name (which misses acronym/hyphenated domains: OXB->oxb.com,
    'United Imaging'->united-imaging.com). '' if none qualifies."""
    for u in urls:
        if not u or not isinstance(u, str):
            continue
        m = re.match(r"https?://([^/]+)", u.strip())
        if not m or _AGG_HOST_RE.search(m.group(1)):
            continue
        return f"https://{m.group(1)}"
    return ""


def _job(jid, title, company, url, location, description="", company_url=""):
    title = (title or "").strip()
    if not title or not jid:
        return None
    j = {"id": jid, "title": title[:120], "company": (company or "").strip()[:80],
         "url": url or "", "location": (location or "").strip()[:80],
         "description": (description or "")[:4000]}
    if company_url:
        j["company_url"] = company_url
    return j


# ─── LinkedIn ────────────────────────────────────────────────────────────
#
# Three markup generations, all seen in the wild (live DOM and Ctrl+S saves):
#   1. current obfuscated classes: job anchors carry no stable class names,
#      but their visible strings are [Title, "Company · Location", "Posted…"];
#   2. classic authed cards (.job-card-container / artdeco lockups);
#   3. guest/logged-out cards (.base-card).
# Detail pages are parsed from the <title> tag ("Job | Company | LinkedIn"),
# the "(Remote/Hybrid/On-site)" location string, and the "About the job"
# section. NOTE: "Top job picks" collection pages are virtualized — a Ctrl+S
# save contains almost no job data; save Job tracker / search / detail pages.

_MODE_RE = re.compile(r"\((Remote|Hybrid|On-site)\)")
_NONTITLE_RE = re.compile(r"^(apply|easy apply|save|saved|dismiss|x)$", re.I)


def _split_company_loc(text):
    company, _, location = text.partition("\u00b7")
    return company.strip(), location.strip()


def parse_linkedin(soup, page_url=""):
    jobs = []
    # Job anchors (generations 1 + 2). Visible strings first; classic-card
    # selectors as fallback for the older markup.
    for a in soup.select("a[href*='/jobs/view/']"):
        m = _LI_VIEW_RE.search(a.get("href", ""))
        if not m:
            continue
        parts = list(a.stripped_strings)
        if not parts or _NONTITLE_RE.match(parts[0]):
            continue
        title = re.sub(r"(.+?)\1$", r"\1", parts[0])   # LinkedIn doubles titles
        company = location = ""
        for p in parts[1:4]:
            if "\u00b7" in p:
                company, location = _split_company_loc(p)
                break
        card = a.find_parent("li") or a.parent
        if not company and card is not None:
            company = _sel(card, ".artdeco-entity-lockup__subtitle",
                           ".job-card-container__primary-description",
                           "h4.base-search-card__subtitle")
            location = location or _sel(card, ".job-card-container__metadata-wrapper li",
                                        ".artdeco-entity-lockup__caption",
                                        "span.job-search-card__location")
        j = _job(f"linkedin_{m.group(1)}", title, company,
                 f"https://www.linkedin.com/jobs/view/{m.group(1)}/", location)
        if j:
            jobs.append(j)

    # Guest cards (generation 3): title/company live outside the anchor.
    for c in soup.select("div.base-card"):
        a = c.select_one("a[href*='/jobs/view/']")
        if not a:
            continue
        m = _LI_VIEW_RE.search(a.get("href", ""))
        title = _sel(c, "h3.base-search-card__title")
        j = _job(f"linkedin_{m.group(1)}" if m else f"linkedin_{stable_id(title)}",
                 title, _sel(c, "h4.base-search-card__subtitle"),
                 a.get("href", "").split("?")[0], _sel(c, "span.job-search-card__location"))
        if j:
            jobs.append(j)

    # Detail page: <title> is "Job Title | Company | LinkedIn" (with an
    # unread-count "(9) " prefix on live DOM). No stable numeric id is
    # recoverable, so the id hashes title+company.
    t = soup.title.get_text(" ", strip=True) if soup.title else ""
    tm = re.match(r"^(?:\(\d+\)\s*)?(.+?)\s*\|\s*(.+?)\s*\|\s*LinkedIn$", t)
    if tm:
        title, company = tm.group(1), tm.group(2)
        loc_el = soup.find(string=_MODE_RE)
        location = re.sub(r"\s+", " ", str(loc_el)).strip() if loc_el else ""
        desc, marker = "", soup.find(string=re.compile(r"^\s*About the job\s*$"))
        sec = marker.find_parent() if marker else None
        for _ in range(5):
            if sec is None or len(sec.get_text(" ", strip=True)) > 400:
                break
            sec = sec.parent
        if sec is not None:
            desc = sec.get_text(" ", strip=True)
        j = _job(f"linkedin_{stable_id(title, company)}", title, company,
                 page_url or "", location, desc)
        if j:
            twin = next((x for x in jobs
                         if x["title"].lower() == j["title"].lower()
                         and not x["company"]), None)
            if twin is not None:
                # Same job seen as a bare anchor: keep its numeric id/url,
                # take the rich fields from the title-tag parse.
                twin.update(company=j["company"], location=j["location"],
                            description=j["description"])
            else:
                jobs.append(j)
    return jobs


# ─── Indeed ──────────────────────────────────────────────────────────────

def parse_indeed(soup, page_url=""):
    jobs = []
    for c in soup.select("div.job_seen_beacon, td.resultContent"):
        a = c.select_one("h2 a[href], a.jcs-JobTitle")
        if not a:
            continue
        href = a.get("href", "")
        m = _INDEED_JK_RE.search(href) or re.search(r"jk=([0-9a-f]+)", str(a.get("data-jk", "")))
        jid = (m.group(1) if m else a.get("data-jk")) or stable_id(href, _txt(a))
        j = _job(f"indeed_{jid}", _txt(a),
                 _sel(c, "[data-testid='company-name']", "span.companyName"),
                 href if href.startswith("http") else f"https://www.indeed.com{href}",
                 _sel(c, "[data-testid='text-location']", "div.companyLocation"))
        if j:
            jobs.append(j)
    return jobs


# ─── Meta Careers (metacareers.com) ──────────────────────────────────────
#
# Meta's careers site is custom-built (not a standard ATS) and blocks
# server-side fetches, so the ONLY way in is a live browser capture
# (userscript button or Ctrl+S). Job links are stable —
#   /profile/job_details/<numeric id>/
# — but Meta's CSS classes are obfuscated, so titles come from the link text
# and locations from a "City, ST" / "Remote" heuristic rather than selectors.
# (The local-tech NC gate still applies downstream, so out-of-NC Meta roles
# are dropped at ingest — as intended.)

_META_JOB_RE = re.compile(r"/profile/job_details/(\d+)")
_META_LOC_RE = re.compile(
    r"([A-Z][A-Za-z.\-]+(?:\s[A-Z][A-Za-z.\-]+)*,\s*[A-Z]{2}\b"
    r"|Remote(?:,\s*[A-Za-z .]+)?|Multiple Locations)")


def parse_metacareers(soup, page_url=""):
    jobs, seen = [], set()
    # Listing/search page: one card per job, each linking to a job_details URL.
    for a in soup.select("a[href*='/profile/job_details/']"):
        m = _META_JOB_RE.search(a.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        jid = m.group(1)
        seen.add(jid)
        card = a.find_parent(["div", "li"]) or a
        strings = [s for s in a.stripped_strings if not _NONTITLE_RE.match(s)]
        title = strings[0] if strings else ""
        if not title:
            te = card.find(["h1", "h2", "h3", "h4"])
            title = _txt(te) if te else ""
        # Search for the location in the card text with the TITLE removed —
        # titles like "Engineer, Reality Labs" carry their own comma and would
        # otherwise bleed into the greedy "City, ST" match.
        rest = card.get_text(" ", strip=True).replace(title, " ", 1) if title else \
            card.get_text(" ", strip=True)
        lm = _META_LOC_RE.search(rest)
        j = _job(f"meta_{jid}", title, "Meta",
                 f"https://www.metacareers.com/profile/job_details/{jid}/",
                 lm.group(1) if lm else "")
        if j:
            jobs.append(j)

    # Single job-detail page: emit/enrich from the title tag + og:description.
    dm = _META_JOB_RE.search(page_url or "")
    if dm:
        jid = dm.group(1)
        og = soup.select_one("meta[property='og:title'][content]")
        raw = (og.get("content") if og else "") or \
            (soup.title.get_text(" ", strip=True) if soup.title else "")
        title = re.sub(r"\s*[|\-–—]\s*Meta\b.*$", "", raw).strip() or raw
        ogd = soup.select_one("meta[property='og:description'][content]")
        desc = ogd.get("content", "") if ogd else ""
        body = soup.get_text(" ", strip=True).replace(title, " ", 1) if title else \
            soup.get_text(" ", strip=True)
        lm = _META_LOC_RE.search(body)
        j = _job(f"meta_{jid}", title, "Meta",
                 f"https://www.metacareers.com/profile/job_details/{jid}/",
                 lm.group(1) if lm else "", desc)
        if j:
            twin = next((x for x in jobs if x["id"] == j["id"]), None)
            if twin:  # listing card + open detail: keep the richer fields
                for k, v in j.items():
                    if v and len(str(v)) > len(str(twin.get(k) or "")):
                        twin[k] = v
            else:
                jobs.append(j)
    return jobs


# ─── Generic (JSON-LD + job-link sweep) ──────────────────────────────────

def parse_jsonld(soup, page_url=""):
    jobs = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else \
            data.get("itemListElement", [data]) if isinstance(data, dict) else []
        for it in items:
            jp = it.get("item", it) if isinstance(it, dict) else {}
            if not isinstance(jp, dict) or jp.get("@type") not in ("JobPosting",):
                continue
            org = jp.get("hiringOrganization") or {}
            loc = jp.get("jobLocation") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
            location = ", ".join(x for x in (addr.get("addressLocality"),
                                             addr.get("addressRegion")) if x)
            url = jp.get("url") or page_url
            # schema.org marks the employer's own site in hiringOrganization
            # (sameAs / url) — capture it as the lead's careers_url hint.
            org_site = ""
            if isinstance(org, dict):
                same = org.get("sameAs")
                same = same if isinstance(same, list) else [same]
                org_site = _company_site(*same, org.get("url"))
            j = _job(f"cap_{stable_id(url, jp.get('title'))}",
                     (jp.get("title") or jp.get("name") or ""),
                     org.get("name", "") if isinstance(org, dict) else str(org),
                     url, location,
                     re.sub(r"<[^>]+>", " ", jp.get("description") or ""),
                     company_url=org_site)
            if j:
                jobs.append(j)
    return jobs


def parse_generic(soup, page_url=""):
    from .fetchers.company import find_job_links
    root_m = re.match(r"https?://[^/]+", page_url or "")
    root = root_m.group(0) if root_m else ""
    jobs = []
    for a, href, title in find_job_links(soup):
        url = href if href.startswith("http") else root + href
        j = _job(f"cap_{stable_id(url)}", title, "", url, "")
        if j:
            jobs.append(j)
    return jobs


# ─── Entry point ─────────────────────────────────────────────────────────

def _canonical_url(soup):
    el = soup.select_one("link[rel='canonical'][href]") \
        or soup.select_one("meta[property='og:url'][content]")
    return (el.get("href") or el.get("content") or "") if el else ""


def parse_page(url, html):
    """Parse captured page HTML -> (jobs, source_label). Layered parsers;
    de-duplicated by job id, site-specific hits first. When `url` is empty
    (Ctrl+S saves carry none), the site is detected from the canonical URL
    or distinctive DOM markers instead."""
    soup = BeautifulSoup(html, "html.parser")
    if not url:
        url = _canonical_url(soup)
    low = (url or "").lower()
    if not low.startswith("http"):
        t = soup.title.get_text(strip=True) if soup.title else ""
        if t.endswith("LinkedIn") or soup.select_one(
                "a[href*='linkedin.com/jobs/view/'], [data-occludable-job-id], "
                ".job-card-container, .base-search-card__title") or \
                soup.select_one("link[href*='licdn.com'], img[src*='licdn.com']"):
            low = "linkedin."
        elif soup.select_one("div.job_seen_beacon, a.jcs-JobTitle"):
            low = "indeed."
        elif soup.select_one("a[href*='/profile/job_details/']"):
            low = "metacareers."
    if "linkedin." in low:
        # Site-specific pages skip the generic link sweep — it would re-add
        # the same postings under synthetic ids.
        layers, source = [parse_linkedin, parse_jsonld], "linkedin"
    elif "indeed." in low:
        layers, source = [parse_indeed, parse_jsonld], "indeed"
    elif "metacareers." in low:
        layers, source = [parse_metacareers], "metacareers"
    else:
        layers, source = [parse_jsonld, parse_generic], "page"

    by_id = {}
    for layer in layers:
        try:
            found = layer(soup, url)
        except Exception as e:
            print(f"    [!] {layer.__name__}: {e}")
            found = []
        seen_urls = {(jj.get("url") or "").split("?")[0].rstrip("/")
                     for jj in by_id.values()}
        for j in found:
            prev = by_id.get(j["id"])
            if prev is None:
                u = (j.get("url") or "").split("?")[0].rstrip("/")
                if u and u in seen_urls:
                    continue                      # same posting, later layer
                by_id[j["id"]] = j
            else:
                # Same job seen twice (e.g. results card + open detail pane):
                # merge, keeping the richer field from either.
                for k, v in j.items():
                    if v and len(str(v)) > len(str(prev.get(k) or "")):
                        prev[k] = v
    return list(by_id.values()), source
