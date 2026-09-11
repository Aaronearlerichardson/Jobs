"""Discourse forum job-category feed."""

from src.net.http import HEADERS, get_json


def fetch_discourse(display_name, base_url, category_id, gate=None):
    url = f"{base_url}/c/job-opportunities/{category_id}.json"
    dsc_headers = {**HEADERS, "Accept": "application/json"}
    data = get_json(url, f"Discourse {display_name}", default={},
                    headers=dsc_headers)
    topics = (data.get("topic_list") or {}).get("topics", []) if data else []
    jobs = []
    for t in topics:
        if t.get("posts_count", 0) == 1 and t.get("reply_count", 0) == 0:
            continue
        title = t.get("title", "")
        slug  = t.get("slug", "")
        tid   = t.get("id", "")
        jurl  = f"{base_url}/t/{slug}/{tid}"
        loc   = t.get("last_posted_at", "")[:10] if t.get("last_posted_at") else "See post"
        if gate is None or gate(title):
            jobs.append({
                "id":          f"discourse_{base_url.split('.')[0].split('//')[1]}_{tid}",
                "company":     display_name,
                "title":       title,
                "url":         jurl,
                "location":    f"Posted {loc}",
                "description": "",
            })
    return jobs
