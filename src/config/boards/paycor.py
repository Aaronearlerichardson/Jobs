"""The `paycor` board spec: Paycor Recruiting (formerly Newton Software)."""

from __future__ import annotations

from src.rows import JSON

SPEC: dict[str, JSON] = {
    # The career site, its embed and its feed all carry the tenant's
    # 32-hex clientId, on either of the vendor's two hosts.
    "detect": [{"host": "recruitingbypaycor.com",
                "re": [r"(?i)(?:recruitingbypaycor\.com|newtonsoftware\.com)/career/"
                       r"[a-z]+\.action\?clientId=([0-9a-f]{32})"]}],
    "canary": {"name": "Almac Group", "handle": "8a788267543c64a8015453881fd50633"},
    # The whole board in one Atom feed, bodies included.
    "listing": {
        "url": "https://recruitingbypaycor.com/career/CareerAtomFeed.action",
        "params": {"clientId": "{slug}"},
        "headers": {"Accept": "application/atom+xml"},
        "decoder": {"kind": "atom"},
        "fields": {
            "id": {"format": "paycor_{slug}_{id}"},
            "title": {"of": "title", "transform": "one_line"},
            "url": "link@href",
            # "United States Durham NC 27704": the city and state.
            "location": {"first": [
                {"join": [{"of": "author.name",
                           "transform": r"group:^(?:United States\s+)?(.+?)\s+[A-Z]{2}\s+\d{5}"},
                          {"of": "author.name", "transform": r"group:\s([A-Z]{2})\s+\d{5}"}],
                 "sep": ", "},
                "author.name"]},
            "description": {"of": {"first": ["content", "summary"]}, "transform": "html_text"},
            "posted_at": {"first": ["published", "updated"]},
            "department": "category@term",
        },
    },
    # A pulled posting leaves the feed; there is no detail to ask.
    "closure": {"via": "listing"},
}
