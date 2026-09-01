"""Markdown digest renderers + the posting-age tag.

Track-agnostic: the two digest shapes the crawls produce — the RANKED
digest (fit-ordered table with pipeline/watch sections, written by
store-crawl tracks) and the MATCHES digest (flat surfaced-postings table,
written by sweep tracks) — both take the track config `t` and derive their
tag/filename from it, so any user-defined track gets its own digest.
"""

from datetime import datetime

import config

from .digest import send_gmail


def age_tag(row, today=None):
    """Compact posting-age tag for console/digest rows: 'NEW' the day we
    first see it, else days since posted_at ('6d', '45d!' when stale — a
    45+-day-old posting is often a ghost req). '?' when no date is known.
    Workday dates parsed from 'Posted 30+ Days Ago' are floors, so '30d!'
    there means AT LEAST 30 days."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    if (row.get("first_seen") or "")[:10] == today:
        return "NEW"
    posted = (row.get("posted_at") or "")[:10]
    if not posted:
        return "?"
    try:
        days = (datetime.strptime(today, "%Y-%m-%d")
                - datetime.strptime(posted, "%Y-%m-%d")).days
    except ValueError:
        return "?"
    return f"{days}d!" if days >= 45 else f"{days}d"


def _tag(t):
    return f"[{t['label'].upper()}]"


def write_ranked_digest(ranked, t, watch_hits=None, pipeline=None):
    """Fit-ranked markdown digest for a store-crawl track: pipeline section,
    watched-company section, then the full ranked table."""
    config.REPORT_DIR.mkdir(exist_ok=True)
    path = config.REPORT_DIR / f"{t['id']}_{datetime.now():%Y-%m-%d}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {_tag(t)} Job Digest — {datetime.now():%Y-%m-%d}\n\n")
        if pipeline:
            f.write("## Your pipeline\n\n")
            f.write("Managed with `run_scraper.py --mark DISPOSITION JOB` "
                    "(saved stays in the ranking; the rest live here).\n\n")
            f.write("| Disposition | When | Company | Title | Posting | Note |\n")
            f.write("|---|---|---|---|---|---|\n")
            for p in pipeline:
                state = "CLOSED" if (p.get("status") == "closed") else "open"
                f.write(f"| {p.get('disposition')} | {(p.get('disposition_at') or '')[:10]} "
                        f"| {p.get('company_name')} | [{p.get('title')}]({p.get('url')}) "
                        f"| {state} | {p.get('disposition_note') or ''} |\n")
            f.write("\n")
        if watch_hits:
            f.write("## Watched companies — new postings this run\n\n")
            f.write("Flagged regardless of rank or geography "
                    "(`run_scraper.py --watch NAME` manages the list).\n\n")
            for c, j, in_pipeline in watch_hits:
                note = "scored" if in_pipeline else "listed only, outside local scope"
                f.write(f"- **{c['name']}** — [{j.get('title')}]({j.get('url')}) "
                        f"— {j.get('location') or '?'} *({note})*\n")
            f.write("\n")
        f.write(f"**{len(ranked)} open job(s)** (closed, dismissed, and in-pipeline "
                f"postings excluded), ranked by resume fit "
                f"(combined = sqrt(resume-fit x company-mission), shown for reference). "
                f"Age is days since the board's posting date "
                f"(NEW = first seen today, ! = 45d+ stale, ? = date unknown).\n\n")
        f.write("| Fit | Combined | Age | Company | Mission | Title | Location | Why |\n")
        f.write("|----:|---------:|----:|---------|---------|-------|----------|-----|\n")
        today = datetime.now().strftime("%Y-%m-%d")
        for j in ranked:
            fit = j["resume_fit_score"]
            fs = f"{fit:.2f}" if isinstance(fit, float) else "n/a"
            comb = j.get("combined_score")
            cs = f"{comb:.2f}" if isinstance(comb, float) else "n/a"
            f.write(f"| {fs} | {cs} | {age_tag(j, today)} | {j['company_name']} "
                    f"| {j.get('mission_tier') or '?'} "
                    f"| [{j['title']}]({j['url']}) | {j['location']} | {j.get('fit_reason','')} |\n")
    print(f"  digest -> {path}")
    return path


def new_ranked_rows(ranked, t, new_since=None):
    """The ranked rows first seen on or after `new_since` (default today)
    that score at least the track's `digest_min_fit`.

    >>> t = {"digest_min_fit": 0.4}
    >>> rows = [{"job_id": "a", "first_seen": "2026-09-01",
    ...          "resume_fit_score": 0.7},
    ...         {"job_id": "b", "first_seen": "2026-08-30",
    ...          "resume_fit_score": 0.9},
    ...         {"job_id": "c", "first_seen": "2026-09-01",
    ...          "resume_fit_score": 0.2}]
    >>> [j["job_id"] for j in new_ranked_rows(rows, t, "2026-09-01")]
    ['a']

    Reaching `new_since` further back widens the window; the fit floor
    still applies:

    >>> [j["job_id"] for j in new_ranked_rows(rows, t, "2026-08-30")]
    ['a', 'b']

    A row nobody scored never qualifies, whatever the floor:

    >>> new_ranked_rows([{"first_seen": "2026-09-01",
    ...                   "resume_fit_score": None}], t, "2026-09-01")
    []

    Omitting `new_since` means today, so a run's email carries that run's
    finds:

    >>> from datetime import datetime
    >>> today = datetime.now().strftime("%Y-%m-%d")
    >>> fresh = {"job_id": "d", "first_seen": today, "resume_fit_score": 0.9}
    >>> [j["job_id"] for j in new_ranked_rows([fresh], t)]
    ['d']

    Notes:
        The floor is deliberately separate from the UI's `min_fit_default`.
        The written digest is the whole ranking and is fine to browse; the
        email is an interruption, so it gets a stricter bar.
    """
    since = (new_since or datetime.now().strftime("%Y-%m-%d"))[:10]
    floor = float(t.get("digest_min_fit") or 0.0)
    fresh = []
    for j in ranked or []:
        if (j.get("first_seen") or "")[:10] < since:
            continue
        fit = j.get("resume_fit_score")
        if not isinstance(fit, (int, float)) or fit < floor:
            continue
        fresh.append(j)
    return fresh


def send_ranked_digest(ranked, t, watch_hits=None, pipeline=None,
                       new_since=None):
    """Email a store-crawl track's new ranked rows. True when a message
    actually went out.

    Renders the markdown shape of `write_ranked_digest` — pipeline
    closures, watched-company hits, then a fit-ordered table — restricted
    to `new_ranked_rows`, and sends nothing when there is neither a new row
    nor a watch hit. Enforced by
    tests/test_digest.py::TestSendRankedDigest, which monkeypatches
    `send_gmail` (an SMTP call is not a doctest).

    Notes:
        Postings close fast, so the location-scoped track needs a push
        rather than a page to visit. A daily alert that also fires on quiet
        days stops being read, which is what the silent path is for.
    """
    fresh = new_ranked_rows(ranked, t, new_since)
    hits = list(watch_hits or [])
    since = (new_since or datetime.now().strftime("%Y-%m-%d"))[:10]
    closed = [p for p in (pipeline or [])
              if p.get("status") == "closed"
              and (p.get("closed_at") or "")[:10] >= since]
    if not fresh and not hits:
        print("  No new postings or watch hits - skipping email.")
        return False

    tag = _tag(t)
    date_str = datetime.now().strftime("%Y-%m-%d")
    subject = f"{tag} {len(fresh)} new match(es) - {date_str}"

    md = [f"# {tag} New Postings — {date_str}", ""]
    html = [f"<h2>{tag} New Postings — {date_str}</h2>"]
    if closed:
        md += ["## Pipeline — closed since your last digest", ""]
        html.append("<h3>Pipeline — closed since your last digest</h3><ul>")
        for p in closed:
            md.append(f"- **{p.get('disposition')}** — {p.get('company_name')} "
                      f"— [{p.get('title')}]({p.get('url')}) — posting CLOSED")
            html.append(f"<li><strong>{p.get('disposition')}</strong> — "
                        f"{p.get('company_name')} — "
                        f"<a href='{p.get('url')}'>{p.get('title')}</a> — "
                        f"posting CLOSED</li>")
        md.append("")
        html.append("</ul>")
    if hits:
        md += ["## Watched companies — new postings this run", ""]
        html.append("<h3>Watched companies — new postings this run</h3><ul>")
        for c, j, in_pipeline in hits:
            note = "scored" if in_pipeline else "listed only, outside local scope"
            md.append(f"- **{c['name']}** — [{j.get('title')}]({j.get('url')}) "
                      f"— {j.get('location') or '?'} *({note})*")
            html.append(f"<li><strong>{c['name']}</strong> — "
                        f"<a href='{j.get('url')}'>{j.get('title')}</a> — "
                        f"{j.get('location') or '?'} <em>({note})</em></li>")
        md.append("")
        html.append("</ul>")

    floor = float(t.get("digest_min_fit") or 0.0)
    md.append(f"**{len(fresh)} new job(s)** first seen {since}, "
              f"resume fit >= {floor:.2f}, ranked by fit.")
    md += ["", "| Fit | Age | Company | Title | Location | Why |",
           "|----:|----:|---------|-------|----------|-----|"]
    html.append(f"<p><strong>{len(fresh)} new job(s)</strong> first seen "
                f"{since}, resume fit &gt;= {floor:.2f}.</p>")
    html.append('<table border="1" cellpadding="8" cellspacing="0" '
                'style="border-collapse:collapse;width:100%">'
                "<tr><th>Fit</th><th>Age</th><th>Company</th><th>Title</th>"
                "<th>Location</th><th>Why</th></tr>")
    today = datetime.now().strftime("%Y-%m-%d")
    for j in fresh:
        fit = j.get("resume_fit_score")
        fs = f"{fit:.2f}" if isinstance(fit, float) else "n/a"
        age = age_tag(j, today)
        why = j.get("fit_reason", "") or ""
        md.append(f"| {fs} | {age} | {j.get('company_name')} "
                  f"| [{j.get('title')}]({j.get('url')}) "
                  f"| {j.get('location')} | {why} |")
        html.append(f"<tr><td>{fs}</td><td>{age}</td>"
                    f"<td>{j.get('company_name')}</td>"
                    f"<td><a href='{j.get('url')}'>{j.get('title')}</a></td>"
                    f"<td>{j.get('location')}</td><td>{why}</td></tr>")
    html.append("</table>")

    plain = "\n".join(md) + "\n"
    body = ('<html><body style="font-family:sans-serif;max-width:900px">'
            + "".join(html) + "</body></html>")
    if send_gmail(subject, plain, body):
        print(f"  {tag} digest emailed ({len(fresh)} new, {len(hits)} watch).")
        return True
    return False


def toast(t, count, path):
    """Raise a Windows desktop toast for a just-sent digest. True only when
    one was actually shown.

    Off unless the track sets `notify`, off when the run found nothing, and
    a silent no-op when the optional `winotify` package is missing.
    Enforced by tests/test_digest.py::TestToast.

    Notes:
        The email is the contract and the toast is a convenience, so every
        failure here is swallowed rather than surfaced.
    """
    if not t.get("notify") or not count:
        return False
    try:
        from winotify import Notification
    except Exception:
        return False
    try:
        n = Notification(app_id="Job Crawler",
                         title=f"{_tag(t)} {count} new posting(s)",
                         msg="Open today's digest")
        n.add_actions(label="Open digest", launch=str(path))
        n.show()
        return True
    except Exception:
        return False


def write_matches_digest(matches, report_dir, t):
    """Flat surfaced-postings digest for a sweep track."""
    report_dir.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = report_dir / f"{t['id']}_matches_{date_str}.md"
    tag = _tag(t)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {tag} Job Alert - {date_str}\n\n")
        if not matches:
            f.write("_No matching postings this run._\n")
        else:
            n_remote = sum(1 for j in matches if j.get("remote_eligible"))
            f.write(f"**{len(matches)} posting(s)** "
                    f"({n_remote} remote-eligible; location-agnostic sweep).\n\n")
            with_fit = any(j.get("resume_fit_score") is not None for j in matches)
            fit_h = "Fit | " if with_fit else ""
            f.write(f"| {fit_h}Tag | Company | Title | Location | Anchor | Remote signal |\n")
            f.write(f"|{'----:|' if with_fit else ''}-----|---------|-------|----------|--------|---------------|\n")
            for j in matches:
                fit = j.get("resume_fit_score")
                fit_c = (f"{fit:.2f} | " if isinstance(fit, (int, float))
                         else "n/a | ") if with_fit else ""
                f.write(f"| {fit_c}{tag} | {j.get('company') or j.get('company_name')} | "
                        f"[{j['title']}]({j['url']}) | {j['location']} | "
                        f"{j.get('anchor_signal', '')} | "
                        f"{j.get('remote_signal', '')} |\n")
    return path


def send_matches_digest(matches, t, cfg):
    """Email the matches digest. No-op if creds are unset or no matches."""
    if not matches:
        print("  No matches - skipping email.")
        return
    tag = _tag(t)
    date_str = datetime.now().strftime("%Y-%m-%d")
    subject = f"{tag} {len(matches)} posting(s) - {date_str}"
    plain = "\n".join(
        [subject, ""]
        + [f"- {tag} {j['title']}\n  "
           f"{j.get('company') or j.get('company_name')} | {j['location']}\n"
           f"  {j['url']}\n"
           for j in matches]
    )
    rows = "".join(
        f"<tr><td>{tag}</td><td><a href='{j['url']}'>{j['title']}</a></td>"
        f"<td>{j.get('company') or j.get('company_name')}</td>"
        f"<td>{j['location']}</td></tr>"
        for j in matches
    )
    html = f"""<html><body style="font-family:sans-serif;max-width:760px">
<h2>{tag} Job Alert - {date_str}</h2>
<p><strong>{len(matches)} posting(s)</strong></p>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%">
  <tr><th>Tag</th><th>Title</th><th>Company</th><th>Location</th></tr>{rows}
</table>
</body></html>"""
    if send_gmail(subject, plain, html):
        print(f"  {tag} digest emailed ({len(matches)} posting(s)).")
