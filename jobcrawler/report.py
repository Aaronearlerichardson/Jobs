"""Reports, keyword expansion report, Gmail digest."""

import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD,
    INCLUDE_KEYWORDS,
    LOCATION_EXCLUDE,
    LOCATION_INCLUDE,
    REPORT_DIR,
)
from .claude import expand_search


# ─── Job report ───────────────────────────────────────────────────────────

def write_report(new_jobs):
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = REPORT_DIR / f"jobs_{date_str}.md"
    by_company = {}
    for job in new_jobs:
        by_company.setdefault(job["company"], []).append(job)

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# BCI Job Alert - {date_str}\n\n")
        if not new_jobs:
            f.write("_No new relevant postings since last run._\n")
        else:
            f.write(f"**{len(new_jobs)} new posting(s)**\n\n")
            f.write("| Company | Title | Location |\n|---------|-------|----------|\n")
            for j in new_jobs:
                f.write(f"| {j['company']} | [{j['title']}]({j['url']}) | {j['location']} |\n")
            f.write("\n---\n\n")
            for company, jobs in sorted(by_company.items()):
                f.write(f"## {company}\n\n")
                for j in jobs:
                    f.write(f"### [{j['title']}]({j['url']})\n")
                    f.write(f"**Location:** {j['location']}  \n")
                    if j.get("description"):
                        f.write(f"{j['description'].replace(chr(10),' ').strip()[:400]}...\n")
                    f.write("\n")
    print(f"  Report -> {path}")
    return path


# ─── Email ────────────────────────────────────────────────────────────────

def send_email(new_jobs, report_path):
    if not new_jobs:
        print("  No new jobs - skipping email.")
        return
    if GMAIL_APP_PASSWORD == "YOUR_APP_PASSWORD_HERE":
        print("  [!] Set GMAIL_APP_PASSWORD before emailing.")
        return

    subject = f"[BCI Jobs] {len(new_jobs)} new posting(s) - {datetime.now().strftime('%Y-%m-%d')}"
    plain = "\n".join(
        [subject, ""]
        + [f"- {j['title']}\n  {j['company']} | {j['location']}\n  {j['url']}\n"
           for j in new_jobs]
    )
    rows = "".join(
        f"<tr><td><a href='{j['url']}'>{j['title']}</a></td>"
        f"<td>{j['company']}</td><td>{j['location']}</td></tr>"
        for j in new_jobs
    )
    html = f"""<html><body style="font-family:sans-serif;max-width:700px">
<h2>BCI Job Alert - {datetime.now().strftime('%Y-%m-%d')}</h2>
<p><strong>{len(new_jobs)} new posting(s) found</strong></p>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%">
  <tr><th>Title</th><th>Company</th><th>Location</th></tr>{rows}
</table>
<p>Full report: {report_path}</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = GMAIL_ADDRESS
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            srv.sendmail(GMAIL_ADDRESS, GMAIL_ADDRESS, msg.as_string())
        print("  Email sent.")
    except smtplib.SMTPAuthenticationError:
        print("  [!] Gmail auth failed - check your App Password.")
    except Exception as e:
        print(f"  [!] Email error: {e}")


# ─── Expansion pretty-printers ────────────────────────────────────────────

def print_expansion(term, expanded):
    w = 62
    bar = "=" * w
    print(f"\n{bar}")
    print(f"  BCI Expansion: '{term}'")
    print(f"{bar}")

    titles   = expanded.get("titles",   [])
    keywords = expanded.get("keywords", [])
    sectors  = expanded.get("sectors",  [])

    print(f"\n  JOB TITLES TO SEARCH ({len(titles)})")
    for t in titles:
        print(f"    - {t}")

    print(f"\n  KEYWORDS TO ADD ({len(keywords)})")
    for k in keywords:
        marker = "  [already in list]" if k.lower() in INCLUDE_KEYWORDS else ""
        print(f"    - {k}{marker}")

    print(f"\n  SECTORS / COMPANIES TO INVESTIGATE ({len(sectors)})")
    for s in sectors:
        print(f"    - {s}")

    print(f"\n  {'-'*58}")
    print(f"  To fold these into a live crawl, rerun with:")
    print(f'    python crawler.py --expand-live "{term}"')
    print(f"{bar}\n")


def print_location_expansion(term, expanded):
    w = 62
    bar = "=" * w
    print(f"\n{bar}")
    print(f"  Location Expansion: '{term}'")
    print(f"{bar}")
    include = expanded.get("include", [])
    exclude = expanded.get("exclude", [])

    print(f"\n  LOCATION_INCLUDE additions ({len(include)})")
    for x in include:
        marker = "  [already in list]" if x.lower() in [i.lower() for i in LOCATION_INCLUDE] else ""
        print(f"    - {x}{marker}")

    print(f"\n  LOCATION_EXCLUDE additions ({len(exclude)})")
    for x in exclude:
        marker = "  [already in list]" if x.lower() in [i.lower() for i in LOCATION_EXCLUDE] else ""
        print(f"    - {x}{marker}")

    print(f"\n  {'-'*58}")
    print(f"  Copy entries you want into LOCATION_INCLUDE / LOCATION_EXCLUDE.")
    print(f"{bar}\n")


# ─── Bulk keyword report ──────────────────────────────────────────────────

def generate_keyword_report(delay=0.5):
    """
    Expand every INCLUDE_KEYWORDS entry via Claude, aggregate unique
    new titles/keywords/sectors, write a markdown report.
    """
    REPORT_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = REPORT_DIR / f"keyword_expansion_{date_str}.md"

    existing_kw = {k.lower() for k in INCLUDE_KEYWORDS}
    all_titles, all_keywords, all_sectors = {}, {}, {}

    bar = "=" * 62
    print(f"\n{bar}")
    print(f"  Keyword Report - expanding {len(INCLUDE_KEYWORDS)} keyword(s)")
    print(f"{bar}\n")

    for i, kw in enumerate(INCLUDE_KEYWORDS, 1):
        print(f"  [{i}/{len(INCLUDE_KEYWORDS)}] '{kw}'")
        expanded = expand_search(kw)
        if not expanded:
            continue
        for t in expanded.get("titles", []):
            all_titles.setdefault(t.strip(), []).append(kw)
        for k in expanded.get("keywords", []):
            all_keywords.setdefault(k.strip(), []).append(kw)
        for s in expanded.get("sectors", []):
            all_sectors.setdefault(s.strip(), []).append(kw)
        time.sleep(delay)

    def sort_by_freq(d):
        return sorted(d.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Keyword Expansion Report - {date_str}\n\n")
        f.write(f"Seeded from **{len(INCLUDE_KEYWORDS)}** existing keyword(s) in `INCLUDE_KEYWORDS`.\n")
        f.write(f"Suggestions ranked by how many seed terms surfaced them.\n\n")

        f.write("## New keywords to consider\n\n")
        f.write("Items marked `[already in list]` are in `INCLUDE_KEYWORDS`.\n\n")
        f.write("| Suggestion | Surfaced by | Status |\n|---|---|---|\n")
        for term, seeds in sort_by_freq(all_keywords):
            flag = "already in list" if term.lower() in existing_kw else "NEW"
            f.write(f"| `{term}` | {len(seeds)} | {flag} |\n")

        f.write("\n## Alternative job titles to search\n\n")
        f.write("| Title | Surfaced by |\n|---|---|\n")
        for term, seeds in sort_by_freq(all_titles):
            f.write(f"| {term} | {len(seeds)} |\n")

        f.write("\n## Sectors / employers to investigate\n\n")
        f.write("Pass any of these to `discover.py` to get ATS slug candidates.\n\n")
        f.write("| Sector / Employer | Surfaced by |\n|---|---|\n")
        for term, seeds in sort_by_freq(all_sectors):
            f.write(f"| {term} | {len(seeds)} |\n")

        new_only = [t for t in all_keywords if t.lower() not in existing_kw]
        if new_only:
            f.write("\n## Copy-paste block (new keywords only)\n\n")
            f.write("```python\n")
            for t in sorted(new_only, key=str.lower):
                f.write(f'    "{t.lower()}",\n')
            f.write("```\n")

    print(f"\n  Report -> {path}\n")
    return path
