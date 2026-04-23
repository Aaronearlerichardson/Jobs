"""ATS slug probes — cheap HEAD/GET checks to confirm a slug is real."""

import requests

from ..http import HEADERS


def probe_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        return (True, len(r.json().get("jobs", [])))
    except Exception:
        return (False, 0)


def probe_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        data = r.json()
        return (True, len(data) if isinstance(data, list) else 0)
    except Exception:
        return (False, 0)


def probe_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        if r.status_code != 200:
            return (False, 0)
        return (True, len(r.json().get("jobPostings", [])))
    except Exception:
        return (False, 0)


def probe_kula(slug):
    url = f"https://careers.kula.ai/{slug}"
    try:
        r = requests.get(url, timeout=10, headers=HEADERS)
        return (r.status_code == 200 and len(r.text) > 1000, 0)
    except Exception:
        return (False, 0)


PROBES = {
    "greenhouse": probe_greenhouse,
    "lever":      probe_lever,
    "ashby":      probe_ashby,
    "kula":       probe_kula,
}
