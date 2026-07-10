"""Discourse forum job-category feed."""

import requests

from ..filters import is_relevant
from ..http import HEADERS


def fetch_discourse(display_name, base_url, category_id):
    url = f"{base_url}/c/job-opportunities/{category_id}.json"
    dsc_headers = {**HEADERS, "Accept": "application/json"}
    try:
        r = requests.get(url, timeout=20, headers=dsc_headers)
        r.raise_for_status()
    except Exception as e:
        print(f"    [!] Discourse {display_name}: {e}")
        return []

    topics = r.json().get("topic_list", {}).get("topics", [])
    jobs = []
    for t in topics:
        if t.get("posts_count", 0) == 1 and t.get("reply_count", 0) == 0:
            continue
        title = t.get("title", "")
        slug  = t.get("slug", "")
        tid   = t.get("id", "")
        jurl  = f"{base_url}/t/{slug}/{tid}"
        loc   = t.get("last_posted_at", "")[:10] if t.get("last_posted_at") else "See post"
        if is_relevant(title):
            jobs.append({
                "id":          f"discourse_{base_url.split('.')[0].split('//')[1]}_{tid}",
                "company":     display_name,
                "title":       title,
                "url":         jurl,
                "location":    f"Posted {loc}",
                "description": "",
            })
    return jobs
