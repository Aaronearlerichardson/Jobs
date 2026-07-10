"""REMOTE-NEURAL track.

Surfaces REMOTE-eligible roles that keep all three of: neural signals
(EEG/iEEG/ECoG/MEG/BCI/neural decoding/neural signal processing), a high
technical bar (ML / PyTorch / signal processing), and a clinical/health
mission.

How it stays modular:
  * Keyword focus is applied by *mutating config's tier lists in place*
    (``apply_to_config``), so the shared fetchers' ``is_relevant`` picks up
"""

import re
from datetime import datetime

from ..digest import send_gmail

from .. import store as store_mod
from ..fetchers import (
    fetch_discourse,
    fetch_hnhiring,
    fetch_remoteok,
    fetch_remotive,
    fetch_rss,
    fetch_websearch,
)
from ..sources import ATS_REGISTRY, LIGHTWEIGHT, iter_config_sources, iter_store_sources

TAG = "[REMOTE-NEURAL]"
TRACK = "remote-neural"


# =========================================================================
#  KEYWORD FOCUS — tier model as in filters.is_relevant: CORE alone passes
#  (a neural-signal term is a strong signal); DOMAIN+SKILL passes (clinical
#  mission + technical skill, for roles that don't name a modality).
# =========================================================================

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
# is_relevant() alone passes DOMAIN+SKILL roles without any neural term, so
# the runner also requires a neural signal. Short acronym anchors (eeg, bci,
# ecog...) need word boundaries — "ecog" fires inside "recognized" — while
# longer anchors stay substring so "subcortical" still matches "cortical".
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
# Neural employers carry their modality in every posting's boilerplate, so
# "Corporate Controller" would ride in on the EEG mission statement; the
# title must read technical. Inclusive by design.
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

# Prioritized company targets, fetched (and deduped) first.
PRIORITY_COMPANIES = [
    ("Beacon Biosignals",      "greenhouse", "beaconbiosignals"),
    ("Precision Neuroscience", "kula",       "precision-neuroscience"),
    ("Paradromics",            "jazzhr",     "paradromicsinc"),
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


def build_sources(cfg, include_websearch=True):
    """Assemble the ordered list of (name, platform, thunk) source specs:
    priority companies, then the company store (tag: neural), then the
    config seed lists — deduped in that order so cross-source duplicates
    resolve deterministically. Heavy onsite ATSes (Workday/SuccessFactors/
    PeopleAdmin) are deliberately excluded: they're the local track's
    locality-bound employers, and the remote filter would cull nearly all
    of their thousands of onsite reqs anyway.
    """
    sources, used = [], set()

    def add(ats, name, slug, thunk, star=""):
        key = (ats, str(slug))
        if key not in used:
            used.add(key)
            sources.append((name, ats + star, thunk))

    # 1) Priority targets.
    for name, ats, slug in PRIORITY_COMPANIES:
        _, _, mk, _, _ = ATS_REGISTRY[ats]
        add(ats, name, slug, mk(name, slug), star="*")

    # 2) Company store sweep (populated by --import-seeds + discovery).
    try:
        conn = store_mod.connect()
        rows = store_mod.get_companies(conn, active_only=True, tag="neural")
        conn.close()
    except Exception as e:
        print(f"  [!] company store unavailable ({e}); using config lists only")
        rows = []
    for ats, name, slug, thunk in iter_store_sources(rows):
        add(ats, name, slug, thunk)

    # 3) Config seed lists (lightweight ATSes only), deduped against the store.
    for ats, name, slug, thunk, _pause in iter_config_sources(cfg, only=LIGHTWEIGHT):
        add(ats, name, slug, thunk)

    # 4) Forums + aggregator feeds (remote-native boards).
    for name, base, cat in cfg.DISCOURSE_BOARDS:
        sources.append((name, "discourse",
                        lambda n=name, b=base, c=cat: fetch_discourse(n, b, c)))
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

    # 5) Web searches (DDG -> JSON-LD).
    if include_websearch:
        for label, query, n in WEBSEARCH_QUERIES:
            sources.append((label, "websearch",
                            lambda l=label, q=query, m=n:
                                fetch_websearch(l, q, max_results=m)))

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
            with_fit = any(j.get("resume_fit_score") is not None for j in jobs)
            fit_h = "Fit | " if with_fit else ""
            f.write(f"| {fit_h}Tag | Company | Title | Location | Neural | Remote signal |\n")
            f.write(f"|{'----:|' if with_fit else ''}-----|---------|-------|----------|--------|---------------|\n")
            for j in jobs:
                fit = j.get("resume_fit_score")
                fit_c = (f"{fit:.2f} | " if isinstance(fit, (int, float)) else "n/a | ") if with_fit else ""
                f.write(f"| {fit_c}{TAG} | {j['company']} | "
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
    if send_gmail(subject, plain, html):
        print(f"  {TAG} digest emailed ({len(jobs)} posting(s)).")
