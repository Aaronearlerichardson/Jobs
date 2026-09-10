"""Digest renderers, the Gmail sender, and the posting-age tag.

Track-agnostic: the two digest shapes the crawls produce — the RANKED
digest (fit-ordered table with pipeline/watch sections, written by
store-crawl tracks) and the MATCHES digest (flat surfaced-postings table,
written by sweep tracks) — both take the track config `t` and derive their
tag/filename from it, so any user-defined track gets its own digest.

Every digest, written or emailed, is a title plus a list of SECTIONS, each
`(heading, intro, body)` with the body a table or a bullet list whose rows
are already `(markdown, html)` pairs. `_render` turns that list into the
markdown document and the HTML document in one pass, so a file and the
email built from the same sections cannot drift apart. The row and cell
builders (`_link`, `_fit`, `_cells`, `_bullet`, ...) are the pieces the
four public renderers compose.
"""

import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src import config

from src.match import locality

# The mid-fit local band, half-open on the high side. The interviews to date
# came from applications scored in this range at local onsite postings, not
# from the top of the ranking, so the digest and the Jobs tab both surface
# it as the place to send application volume.
APPLY_BAND = (0.40, 0.70)
APPLY_BAND_LIMIT = 10


def age_tag(row, today=None):
    """Compact posting-age tag for console/digest rows: 'NEW' the day we
    first see it, else days since posted_at ('6d', '45d!' when stale — a
    45+-day-old posting is often a ghost req). '?' when no date is known.
    Workday dates parsed from 'Posted 30+ Days Ago' are floors, so '30d!'
    there means AT LEAST 30 days."""
    today = today or _today()
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


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _tag(t):
    return f"[{t['label'].upper()}]"


def apply_band_rows(ranked, limit=APPLY_BAND_LIMIT):
    """The undecided open local rows scored inside APPLY_BAND, best fit
    first, at most `limit` of them.

    Local means the location string matches the configured locality
    (src.match.locality.NC_RE), the same test the web UI's geo bucket applies at
    serve time; remote and relocation rows stay out however well they
    score, and so does anything the user has already decided on (saved
    included). Enforced by tests/test_digest.py::TestApplyBand.
    """
    lo, hi = APPLY_BAND
    picked = []
    for j in ranked or []:
        fit = j.get("resume_fit_score")
        if not isinstance(fit, (int, float)) or not (lo <= fit < hi):
            continue
        if (j.get("status") or "open") == "closed" or j.get("disposition"):
            continue
        if not locality.NC_RE.search(j.get("location") or ""):
            continue
        picked.append(j)
    picked.sort(key=lambda j: j["resume_fit_score"], reverse=True)
    return picked[:limit]


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
    since = (new_since or _today())[:10]
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


# --------------------------------------------------------------------------- #
#  Cells, rows, sections
# --------------------------------------------------------------------------- #
#
# A cell is a plain value (rendered the same in markdown and HTML, `None`
# included, as str() of it) or a `(markdown, html)` pair for anything with
# markup: links, bold, emphasis. Rows are built from cells and are pairs
# too; a section body is a pair for the whole table or list.

def _pair(cell):
    return cell if isinstance(cell, tuple) else (str(cell), str(cell))


def _bold(text):
    return f"**{text}**", f"<strong>{text}</strong>"


def _link(j):
    """The posting's title linked to its URL."""
    title, url = j.get("title"), j.get("url")
    return f"[{title}]({url})", f"<a href='{url}'>{title}</a>"


def _fit(score):
    """A fit/combined score to two places; 'n/a' when nobody scored it."""
    return f"{score:.2f}" if isinstance(score, (int, float)) else "n/a"


def _company(j):
    """Sweep rows carry `company`, store rows `company_name`."""
    return j.get("company") or j.get("company_name")


def _cells(cells):
    """One table row."""
    md, html = zip(*map(_pair, cells))
    return ("| " + " | ".join(md) + " |",
            "<tr>" + "".join(f"<td>{c}</td>" for c in html) + "</tr>")


def _bullet(parts):
    """One list item: the first part bold, the parts joined by em dashes."""
    md, html = zip(*map(_pair, parts))
    bmd, bhtml = _bold(md[0]), _bold(html[0])
    return ("- " + " — ".join((bmd[0],) + md[1:]),
            "<li>" + " — ".join((bhtml[1],) + html[1:]) + "</li>")


def _table(cols, rows, numeric=()):
    """A table body. `numeric` names the right-aligned columns; a table with
    any gets padded `----:` markdown separators, one with none keeps the bare
    `|---|` form. Both render identically; the split keeps the written files
    diff-stable across runs."""
    if numeric:
        sep = "|" + "|".join("-" * (len(c) + 1) + ":" if c in numeric
                             else "-" * (len(c) + 2) for c in cols) + "|"
    else:
        sep = "|" + "---|" * len(cols)
    md = ["| " + " | ".join(cols) + " |", sep] + [r[0] for r in rows]
    html = ('<table border="1" cellpadding="8" cellspacing="0" '
            'style="border-collapse:collapse;width:100%">'
            + "<tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"
            + "".join(r[1] for r in rows) + "</table>")
    return "\n".join(md), html


def _list(rows):
    """A bullet-list body."""
    return "\n".join(r[0] for r in rows), "<ul>" + "".join(r[1] for r in rows) + "</ul>"


def _render(title, sections):
    """(markdown, html) documents for a title and its sections. A section is
    `(heading, intro, body)`; any part may be None. The HTML is the body
    only — `_html_doc` wraps it for mail."""
    md, html = [f"# {title}\n"], [f"<h2>{title}</h2>"]
    for heading, intro, body in sections:
        parts = []
        if heading:
            parts.append(f"## {heading}")
            html.append(f"<h3>{heading}</h3>")
        if intro:
            imd, ihtml = _pair(intro)
            parts.append(imd)
            html.append(f"<p>{ihtml}</p>")
        if body:
            parts.append(body[0])
            html.append(body[1])
        md.append("\n\n".join(parts) + "\n")
    return "\n".join(md), "".join(html)


def _html_doc(body, width):
    return (f'<html><body style="font-family:sans-serif;max-width:{width}px">'
            f"{body}</body></html>")


def _digest_path(report_dir, name):
    """`name` under `report_dir` (default config.REPORT_DIR), creating it."""
    report_dir = report_dir or config.REPORT_DIR
    report_dir.mkdir(exist_ok=True)
    return report_dir / name


# Sections shared by the written ranked digest and the ranked email.

_FOLLOWUPS = "Follow-ups due"
_APPLY_BAND = "Apply band"
_WATCH = "Watched companies — new postings this run"


def _band_intro(tail=""):
    lo, hi = APPLY_BAND
    return (f"Open local postings scored {lo:.2f} to {hi:.2f} that you have "
            f"not decided on, best fit first.{tail}")


def _watch_section(watch_hits, intro=None):
    rows = []
    for c, j, in_pipeline in watch_hits:
        note = "scored" if in_pipeline else "listed only, outside local scope"
        loc = j.get("location") or "?"
        rows.append(_bullet([c["name"], _link(j),
                             (f"{loc} *({note})*", f"{loc} <em>({note})</em>")]))
    return _WATCH, intro, _list(rows)


# --------------------------------------------------------------------------- #
#  Ranked digest (store-crawl tracks)
# --------------------------------------------------------------------------- #

def write_ranked_digest(ranked, t, watch_hits=None, pipeline=None,
                        followups=None, report_dir=None, triage=None):
    """Fit-ranked markdown digest for a store-crawl track: pipeline section,
    follow-ups due, apply band, watched-company section, then the full
    ranked table. `followups` is `store.followups_due` output; omitted, the
    section is skipped. `triage` is `store.triage_counts` output (the
    harvest funnel, rows per gate); omitted or empty, no section. Written
    under `report_dir` (default config.REPORT_DIR); returns the path."""
    today = _today()
    sections = []
    if pipeline:
        rows = [_cells([p.get("disposition"),
                        (p.get("disposition_at") or "")[:10],
                        p.get("company_name"), _link(p),
                        "CLOSED" if p.get("status") == "closed" else "open",
                        p.get("disposition_note") or ""])
                for p in pipeline]
        sections.append((
            "Your pipeline",
            "Managed with `run_scraper.py --mark DISPOSITION JOB` "
            "(saved stays in the ranking; the rest live here).",
            _table(("Disposition", "When", "Company", "Title", "Posting",
                    "Note"), rows)))
    if followups:
        rows = [_cells([(p.get("followup_at") or "")[:10],
                        p.get("company_name"), _link(p),
                        p.get("contact") or "", p.get("disposition")])
                for p in followups]
        sections.append((
            _FOLLOWUPS,
            "Live applications whose follow-up date has arrived "
            "(set in the Pipeline tab).",
            _table(("Due", "Company", "Title", "Contact", "Disposition"),
                   rows)))
    band = apply_band_rows(ranked)
    if band:
        rows = [_cells([_fit(j["resume_fit_score"]), age_tag(j, today),
                        j.get("company_name"), _link(j), j.get("location")])
                for j in band]
        sections.append((
            _APPLY_BAND,
            _band_intro(" Interviews have come from this band rather than "
                        "the top of the ranking, so this is where "
                        "application volume goes."),
            _table(("Fit", "Age", "Company", "Title", "Location"), rows,
                   numeric={"Fit", "Age"})))
    if watch_hits:
        sections.append(_watch_section(
            watch_hits,
            "Flagged regardless of rank or geography "
            "(`run_scraper.py --watch NAME` manages the list)."))
    if triage:
        sections.append((
            "Harvest triage, last 7 days",
            "Harvested rows by the gate that decided them (`ok` surfaced "
            "into the ranking; `fit` scored under the digest floor; the "
            "rest never cost a fetch or a score).",
            _table(tuple(triage), [_cells([str(n) for n in triage.values()])],
                   numeric=set(triage))))
    rows = [_cells([_fit(j.get("resume_fit_score")),
                    _fit(j.get("combined_score")), age_tag(j, today),
                    j.get("company_name"), j.get("mission_tier") or "?",
                    _link(j), j.get("location"), j.get("fit_reason") or ""])
            for j in ranked]
    sections.append((
        None,
        f"**{len(ranked)} open job(s)** (closed, dismissed, and in-pipeline "
        f"postings excluded), ranked by resume fit "
        f"(combined = sqrt(resume-fit x company-mission), shown for reference). "
        f"Age is days since the board's posting date "
        f"(NEW = first seen today, ! = 45d+ stale, ? = date unknown).",
        _table(("Fit", "Combined", "Age", "Company", "Mission", "Title",
                "Location", "Why"), rows,
               numeric={"Fit", "Combined", "Age"})))

    md, _ = _render(f"{_tag(t)} Job Digest — {today}", sections)
    path = _digest_path(report_dir, f"{t['id']}_{today}.md")
    path.write_text(md, encoding="utf-8")
    print(f"  digest -> {path}")
    return path


def send_ranked_digest(ranked, t, watch_hits=None, pipeline=None,
                       new_since=None, followups=None):
    """Email a store-crawl track's new ranked rows. True when a message
    actually went out.

    The sections of `write_ranked_digest` in brief — pipeline rows closed
    since `new_since`, follow-ups due, the apply band, watched-company
    hits, then a fit-ordered table restricted to `new_ranked_rows` — and
    nothing is sent when there is neither a new row nor a watch hit (the
    follow-up and apply-band sections ride along; they do not trigger a
    send on their own). Enforced by tests/test_digest.py::TestSendRankedDigest,
    which monkeypatches `_send_gmail` (an SMTP call is not a doctest).

    Notes:
        Postings close fast, so the location-scoped track needs a push
        rather than a page to visit. A daily alert that also fires on quiet
        days stops being read, which is what the silent path is for.
    """
    fresh = new_ranked_rows(ranked, t, new_since)
    hits = list(watch_hits or [])
    since = (new_since or _today())[:10]
    closed = [p for p in (pipeline or [])
              if p.get("status") == "closed"
              and (p.get("closed_at") or "")[:10] >= since]
    if not fresh and not hits:
        print("  No new postings or watch hits - skipping email.")
        return False

    tag, today = _tag(t), _today()
    sections = []
    if closed:
        rows = [_bullet([p.get("disposition"), p.get("company_name"),
                         _link(p), "posting CLOSED"]) for p in closed]
        sections.append(("Pipeline — closed since your last digest", None,
                         _list(rows)))
    if followups:
        rows = []
        for p in followups:
            who = f" ({p.get('contact')})" if p.get("contact") else ""
            link = _link(p)
            rows.append(_bullet([(p.get("followup_at") or "")[:10],
                                 p.get("company_name"),
                                 (link[0] + who, link[1] + who),
                                 p.get("disposition")]))
        sections.append((_FOLLOWUPS, None, _list(rows)))
    band = apply_band_rows(ranked)
    if band:
        rows = [_bullet([_fit(j["resume_fit_score"]), j.get("company_name"),
                         _link(j), j.get("location")]) for j in band]
        sections.append((_APPLY_BAND, _band_intro(), _list(rows)))
    if hits:
        sections.append(_watch_section(hits))
    floor = float(t.get("digest_min_fit") or 0.0)
    n_md, n_html = _bold(f"{len(fresh)} new job(s)")
    rows = [_cells([_fit(j.get("resume_fit_score")), age_tag(j, today),
                    j.get("company_name"), _link(j), j.get("location"),
                    j.get("fit_reason") or ""])
            for j in fresh]
    sections.append((
        None,
        (f"{n_md} first seen {since}, resume fit >= {floor:.2f}, "
         f"ranked by fit.",
         f"{n_html} first seen {since}, resume fit &gt;= {floor:.2f}."),
        _table(("Fit", "Age", "Company", "Title", "Location", "Why"), rows,
               numeric={"Fit", "Age"})))

    plain, html = _render(f"{tag} New Postings — {today}", sections)
    subject = f"{tag} {len(fresh)} new match(es) - {today}"
    if _send_gmail(subject, plain, _html_doc(html, 900)):
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


# --------------------------------------------------------------------------- #
#  Matches digest (sweep tracks)
# --------------------------------------------------------------------------- #

def _matches_sections(matches, tag):
    if not matches:
        return [(None, "_No matching postings this run._", None)]
    n_remote = sum(1 for j in matches if j.get("remote_eligible"))
    n_md, n_html = _bold(f"{len(matches)} posting(s)")
    tail = f" ({n_remote} remote-eligible; location-agnostic sweep)."
    with_fit = any(j.get("resume_fit_score") is not None for j in matches)
    cols = (("Fit",) if with_fit else ()) + (
        "Tag", "Company", "Title", "Location", "Anchor", "Remote signal")
    rows = [_cells(([_fit(j.get("resume_fit_score"))] if with_fit else [])
                   + [tag, _company(j), _link(j), j.get("location"),
                      j.get("anchor_signal", ""), j.get("remote_signal", "")])
            for j in matches]
    return [(None, (n_md + tail, n_html + tail),
             _table(cols, rows, numeric={"Fit"}))]


def write_matches_digest(matches, report_dir, t):
    """Flat surfaced-postings digest for a sweep track, written under
    `report_dir` (default config.REPORT_DIR); returns the path."""
    today, tag = _today(), _tag(t)
    md, _ = _render(f"{tag} Job Alert - {today}",
                    _matches_sections(matches, tag))
    path = _digest_path(report_dir, f"{t['id']}_matches_{today}.md")
    path.write_text(md, encoding="utf-8")
    return path


def send_matches_digest(matches, t, cfg=None):
    """Email the matches digest — the same table `write_matches_digest`
    writes. True when a message went out; a no-op without matches. `cfg`
    is unused (the runner still passes it)."""
    if not matches:
        print("  No matches - skipping email.")
        return False
    today, tag = _today(), _tag(t)
    plain, html = _render(f"{tag} Job Alert - {today}",
                          _matches_sections(matches, tag))
    subject = f"{tag} {len(matches)} posting(s) - {today}"
    if _send_gmail(subject, plain, _html_doc(html, 760)):
        print(f"  {tag} digest emailed ({len(matches)} posting(s)).")
        return True
    return False


# --------------------------------------------------------------------------- #
#  Mail
# --------------------------------------------------------------------------- #

def _send_gmail(subject, plain, html):
    """Send a plain+HTML digest to yourself. Returns True on success;
    no-ops with a hint when the app password is unset."""
    if config.GMAIL_APP_PASSWORD == "YOUR_APP_PASSWORD_HERE":
        print("  [!] Set GMAIL_APP_PASSWORD before emailing.")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = msg["To"] = config.GMAIL_ADDRESS
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            srv.sendmail(config.GMAIL_ADDRESS, config.GMAIL_ADDRESS,
                         msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        print("  [!] Gmail auth failed - check your App Password.")
    except Exception as e:
        print(f"  [!] Email error: {e}")
    return False
