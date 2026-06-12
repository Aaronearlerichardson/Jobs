"""REMOTE-NEURAL track.

Surfaces REMOTE-eligible roles that keep all three of: neural signals
(EEG/iEEG/ECoG/MEG/BCI/neural decoding/neural signal processing), a high
technical bar (ML / PyTorch / signal processing), and a clinical/health
mission.

How it stays modular:
  * Keyword focus is applied by *mutating config's tier lists in place*
    (``apply_to_config``), so the shared fetchers' ``is_relevant`` picks up
    the neural-ML focus without any edit to ``config.py`` or ``filters.py``.
  * Remote-eligibility is enforced by ``jobcrawler.remote_filter`` in the
    track runner, not by the shared onsite ``is_location_allowed``.
  * Sources are assembled here as (name, platform, thunk) specs and run by
    ``track_remote_neural.py``; the default ``crawler.py`` / orchestrator
    path is untouched.
  * The digest is built here and tagged ``[REMOTE-NEURAL]``.

Deliberately excluded: the heavy enterprise onsite ATSes in config
(Workday/SuccessFactors/PeopleAdmin — Medtronic, IQVIA, Duke, UNC, ...).
Those are locality-bound RTP employers and belong to the parallel
local-clinical-ml track; sweeping their thousands of onsite reqs here would
be slow and almost entirely culled by the remote filter.
"""

import re
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..fetchers import (
    fetch_adp,
    fetch_ashby,
    fetch_bamboohr,
    fetch_discourse,
    fetch_greenhouse,
    fetch_hnhiring,
    fetch_jazzhr,
    fetch_kula,
    fetch_lever,
    fetch_remoteok,
    fetch_remotive,
    fetch_rss,
    fetch_websearch,
)

TAG = "[REMOTE-NEURAL]"


# =========================================================================
#  KEYWORD FOCUS (neural signal + high technical bar + clinical mission)
# =========================================================================
#
# Tier model is the shared one in filters.is_relevant:
#   CORE alone  -> relevant (a neural-signal term is itself a strong signal
#                  and these roles are inherently high-bar + mission-driven).
#   DOMAIN + SKILL -> relevant (clinical/health mission + technical skill,
#                  catching neural-ML-adjacent roles that don't name a
#                  modality in the title).

# Tier 1 — neural signals. Standalone signal.
TRACK_CORE = [
    # BCI / neural interfaces
    "bci", "brain-computer", "brain computer", "brain machine",
    "neural interface", "neural interfaces", "neural decoding",
    "neural decoder", "neuroprosthetic", "neuroprosthesis",
    "neurotech", "neural signal", "neural signals",
    "neural signal processing", "neurostimulation", "closed-loop",
    "cortical", "intracortical",
    # Electrophysiology modalities
    "eeg", "ieeg", "ecog", "electrocorticography", "lfp",
    "meg", "magnetoencephalography", "emg", "fnirs",
    "spike sorting", "electrophysiology",
    "neural recording", "neural data", "neuroimaging",
    "neuroscience", "neuroscientist", "computational neuroscience",
    # Tooling the user specifically knows
    "mne-python", "neuralink",
]

# Tier 2 — clinical / health mission. Needs a SKILL pair.
TRACK_DOMAIN = [
    "clinical", "clinic", "health", "healthcare", "medical",
    "digital health", "healthtech", "patient", "neurology",
    "neurological", "epilepsy", "seizure", "sleep", "psychiatry",
    "mental health", "brain health", "therapeutic", "diagnostic",
    "biosignal", "physiological", "implantable", "wearable",
    "medical device", "biomedical",
]

# Tier 3 — high technical bar. Needs a DOMAIN pair.
TRACK_SKILL = [
    "machine learning", "deep learning", "pytorch", "tensorflow",
    "jax", "signal processing", "dsp", "time series",
    "neural network", "ml engineer", "research engineer",
    "applied scientist", "data science", "data scientist",
    "algorithms", "computer vision", "real-time", "embedded",
    "firmware", "numpy", "scipy", "decoding", "modeling",
    "software engineer",
]


# ─── Neural anchor gate ──────────────────────────────────────────────────
#
# The shared is_relevant() also passes DOMAIN+SKILL roles (e.g. "data" +
# "health"), which is right for clinical-ML in general but drops the neural
# axis this track exists to keep. So the runner additionally requires a
# *neural signal* in every surfaced posting.
#
# Acronym anchors (eeg, bci, ecog, ieeg, lfp, meg, emg, fnirs) MUST use word
# boundaries — a plain substring match for "ecog" fires inside "recognized"
# and "meg" inside "omega", which silently injects fraud/identity/data-entry
# roles. Multi-word and longer anchors ("neural decoding", "cortical") stay
# substring so "subcortical" still matches "cortical".
NEURAL_ANCHORS = TRACK_CORE


def _anchor_matches(anchor, text):
    # Short single-token acronyms: word-boundary. Everything else: substring.
    if anchor.isalpha() and len(anchor) <= 5:
        return re.search(rf"\b{re.escape(anchor)}\b", text) is not None
    return anchor in text


def neural_signal(title, description=""):
    """Return the neural-signal term that anchors this posting, or None."""
    text = (title + " " + description).lower()
    for a in NEURAL_ANCHORS:
        if _anchor_matches(a, text):
            return a
    return None


def is_neural_role(title, description=""):
    """True iff the posting carries an actual neural-signal term."""
    return neural_signal(title, description) is not None


# ─── High-technical-bar gate ─────────────────────────────────────────────
#
# A neural-signal employer (Beacon et al.) carries its modality in every
# posting's boilerplate, so "Corporate Controller" / "Clinical Study Ops"
# would ride in on the EEG mission statement. This track wants the
# *technical* roles, so the title must read as an engineering / research /
# ML / data role. Inclusive by design — better to keep a borderline
# engineer than to drop a real one.
_TECH_TITLE_RE = re.compile(
    r"\b("
    r"engineer|engineering|developer|scientist|neuroscientist|researcher|"
    r"research|ml|machine learning|deep learning|ai|algorithm|algorithms|"
    r"software|firmware|hardware|data|analytics|analyst|computational|"
    r"quantitative|programmer|architect|signal processing|decoding|robotics|"
    r"systems|sciences|technologist|informatics|bioinformatics|neurotech|"
    r"devops|sre|reliability|platform|modeling|simulation"
    r")\b",
    re.I,
)


def is_technical_role(title):
    """True iff the title reads as a high-technical-bar role."""
    return bool(_TECH_TITLE_RE.search(title or ""))


def apply_to_config(cfg):
    """Point the shared keyword filter at this track's focus and enable
    remote listings. Mutates the existing list objects in place so
    ``filters.is_relevant`` (which imported them at load time) sees the
    change without a re-import.
    """
    cfg.CORE_KEYWORDS[:] = TRACK_CORE
    cfg.DOMAIN_KEYWORDS[:] = TRACK_DOMAIN
    cfg.SKILL_KEYWORDS[:] = TRACK_SKILL
    cfg.INCLUDE_KEYWORDS[:] = TRACK_CORE + TRACK_DOMAIN + TRACK_SKILL
    cfg.ACCEPT_REMOTE = True


# =========================================================================
#  SOURCES
# =========================================================================

# Prioritized company targets. (display_name, platform, fetch-thunk-args)
# Beacon Biosignals first, then Precision Neuroscience, then Paradromics.
PRIORITY_COMPANIES = [
    ("Beacon Biosignals",     "greenhouse", ("beaconbiosignals",)),
    ("Precision Neuroscience", "kula",       ("precision-neuroscience",)),
    ("Paradromics",           "jazzhr",     ("paradromicsinc",)),
]

# Remote-leaning web searches for general neural-ML roles. (label, query,
# max_results). Each result URL is parsed for JSON-LD JobPosting.
WEBSEARCH_QUERIES = [
    ("Neural-ML on WeWorkRemotely",
     '("neural" OR "BCI" OR "EEG" OR "neurotech" OR "brain-computer") '
     '("engineer" OR "scientist") site:weworkremotely.com', 12),
    ("Neural-ML on Himalayas",
     '("neural" OR "BCI" OR "EEG" OR "neural decoding" OR "neurotech") '
     'site:himalayas.app', 12),
    ("Neural-ML on Remote.co",
     '("neural" OR "BCI" OR "EEG" OR "neuroscience") site:remote.co', 12),
    ("Neural-ML remote on Lever",
     '("neural" OR "BCI" OR "EEG" OR "neural signal") '
     '("remote") site:jobs.lever.co', 12),
    ("Neural-ML remote on Ashby",
     '("neural" OR "BCI" OR "EEG" OR "neural decoding") '
     '("remote") site:jobs.ashbyhq.com', 12),
]


def _greenhouse_thunk(slug, name):
    return lambda: fetch_greenhouse(slug, name)


def _lever_thunk(slug, name):
    return lambda: fetch_lever(slug, name)


def _ashby_thunk(slug, name):
    return lambda: fetch_ashby(slug, name)


def _kula_thunk(slug, name):
    return lambda: fetch_kula(name, slug)


def _jazzhr_thunk(sub, name):
    return lambda: fetch_jazzhr(name, sub)


def _discourse_thunk(name, base, cat):
    return lambda: fetch_discourse(name, base, cat)


def _websearch_thunk(label, query, n):
    return lambda: fetch_websearch(label, query, max_results=n)


def build_sources(cfg, include_websearch=True):
    """Assemble the ordered list of (name, platform, thunk) source specs.

    Priority companies first, then the lightweight existing ATS / forum /
    aggregator sources from config, deduped against the priority targets.
    Returns thunks (zero-arg callables) so the runner controls timing,
    counting, and error isolation.
    """
    sources = []
    used_gh, used_kula, used_jazz = set(), set(), set()

    # 1) Priority company targets.
    for name, platform, fargs in PRIORITY_COMPANIES:
        if platform == "greenhouse":
            used_gh.add(fargs[0])
            sources.append((name, "greenhouse*", _greenhouse_thunk(fargs[0], name)))
        elif platform == "kula":
            used_kula.add(fargs[0])
            sources.append((name, "kula*", _kula_thunk(fargs[0], name)))
        elif platform == "jazzhr":
            used_jazz.add(fargs[0])
            sources.append((name, "jazzhr*", _jazzhr_thunk(fargs[0], name)))

    # 2) General neural-ML sweep across existing lightweight ATS sources.
    for slug, name in cfg.GREENHOUSE_COMPANIES.items():
        if slug in used_gh:
            continue
        sources.append((name, "greenhouse", _greenhouse_thunk(slug, name)))
    for slug, name in cfg.LEVER_COMPANIES.items():
        sources.append((name, "lever", _lever_thunk(slug, name)))
    for slug, name in cfg.ASHBY_COMPANIES.items():
        sources.append((name, "ashby", _ashby_thunk(slug, name)))
    for name, slug in cfg.KULA_COMPANIES:
        if slug in used_kula:
            continue
        sources.append((name, "kula", _kula_thunk(slug, name)))
    for sub, name in getattr(cfg, "JAZZHR_COMPANIES", {}).items():
        if sub in used_jazz:
            continue
        sources.append((name, "jazzhr", _jazzhr_thunk(sub, name)))
    for sub, name in getattr(cfg, "BAMBOOHR_COMPANIES", {}).items():
        sources.append((name, "bamboohr",
                        lambda s=sub, n=name: fetch_bamboohr(s, n)))
    for name, cid, ccid in getattr(cfg, "ADP_COMPANIES", []):
        sources.append((name, "adp",
                        lambda c=cid, cc=ccid, n=name: fetch_adp(c, cc, n)))
    for name, base, cat in cfg.DISCOURSE_BOARDS:
        sources.append((name, "discourse", _discourse_thunk(name, base, cat)))

    # 3) Aggregator feeds (remote-native boards).
    if getattr(cfg, "REMOTEOK_ENABLED", True):
        sources.append(("RemoteOK", "remoteok", fetch_remoteok))
    if getattr(cfg, "REMOTIVE_ENABLED", True):
        sources.append(("Remotive", "remotive",
                        lambda: fetch_remotive(category=cfg.REMOTIVE_CATEGORY)))
    if getattr(cfg, "HNHIRING_ENABLED", True):
        sources.append(("HN Who-is-hiring", "hn",
                        lambda: fetch_hnhiring(max_threads=cfg.HNHIRING_MAX_THREADS)))
    for label, url, default_loc in cfg.RSS_FEEDS:
        # Config's RSS feeds are remote-only boards (WWR, Jobicy) — mark
        # items with a structured remote hint so the runner trusts them.
        is_remote_board = default_loc.strip().lower() == "remote"
        sources.append((label, "rss",
                        lambda l=label, u=url, d=default_loc, rb=is_remote_board:
                            fetch_rss(l, u, default_location=d, remote_board=rb)))

    # 4) Web searches (DDG -> JSON-LD).
    if include_websearch:
        for label, query, n in WEBSEARCH_QUERIES:
            sources.append((label, "websearch", _websearch_thunk(label, query, n)))

    return sources


# =========================================================================
#  TAGGING + DIGEST  (every entry carries the [REMOTE-NEURAL] tag)
# =========================================================================

def tag_job(job, signal=None):
    """Stamp the track tag, the remote_eligible flag, and (optionally) the
    matched remote signal onto a job dict in place."""
    job["track_tag"] = TAG
    job["remote_eligible"] = True
    if signal is not None:
        job["remote_signal"] = signal
    return job


def write_digest(jobs, report_dir):
    """Write a tagged markdown digest for this track's matches."""
    report_dir.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = report_dir / f"remote_neural_{date_str}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {TAG} Remote-Neural Job Alert - {date_str}\n\n")
        if not jobs:
            f.write("_No remote-eligible neural-ML postings this run._\n")
        else:
            f.write(f"**{len(jobs)} remote-eligible posting(s)**\n\n")
            f.write("| Tag | Company | Title | Location | Neural | Remote signal |\n")
            f.write("|-----|---------|-------|----------|--------|---------------|\n")
            for j in jobs:
                f.write(f"| {TAG} | {j['company']} | "
                        f"[{j['title']}]({j['url']}) | {j['location']} | "
                        f"{j.get('neural_signal', '')} | "
                        f"{j.get('remote_signal', '')} |\n")
    return path


def send_digest(jobs, cfg):
    """Email the tagged digest. Every entry — subject and body — carries the
    [REMOTE-NEURAL] tag. No-op if creds are unset or there are no jobs."""
    if not jobs:
        print("  No remote-eligible jobs - skipping email.")
        return
    if cfg.GMAIL_APP_PASSWORD == "YOUR_APP_PASSWORD_HERE":
        print("  [!] Set GMAIL_APP_PASSWORD before emailing.")
        return

    date_str = datetime.now().strftime("%Y-%m-%d")
    subject = f"{TAG} {len(jobs)} remote neural-ML posting(s) - {date_str}"
    plain = "\n".join(
        [subject, ""]
        + [f"- {TAG} {j['title']}\n  {j['company']} | {j['location']}\n  {j['url']}\n"
           for j in jobs]
    )
    rows = "".join(
        f"<tr><td>{TAG}</td><td><a href='{j['url']}'>{j['title']}</a></td>"
        f"<td>{j['company']}</td><td>{j['location']}</td></tr>"
        for j in jobs
    )
    html = f"""<html><body style="font-family:sans-serif;max-width:760px">
<h2>{TAG} Remote Neural-ML Job Alert - {date_str}</h2>
<p><strong>{len(jobs)} remote-eligible posting(s)</strong></p>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%">
  <tr><th>Tag</th><th>Title</th><th>Company</th><th>Location</th></tr>{rows}
</table>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.GMAIL_ADDRESS
    msg["To"] = cfg.GMAIL_ADDRESS
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(cfg.GMAIL_ADDRESS, cfg.GMAIL_APP_PASSWORD)
            srv.sendmail(cfg.GMAIL_ADDRESS, cfg.GMAIL_ADDRESS, msg.as_string())
        print(f"  {TAG} digest emailed ({len(jobs)} posting(s)).")
    except smtplib.SMTPAuthenticationError:
        print("  [!] Gmail auth failed - check your App Password.")
    except Exception as e:
        print(f"  [!] Email error: {e}")
