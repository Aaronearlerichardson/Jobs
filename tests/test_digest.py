"""The emailed digest is a push, not a page: it must carry only what is new
enough and good enough to interrupt someone, and must stay silent otherwise.

Offline by construction — `send_gmail` is monkeypatched in every test, so no
SMTP socket is ever opened.
"""

from datetime import datetime, timedelta

import pytest

import core.digest_md as digest_md


TODAY = datetime.now().strftime("%Y-%m-%d")
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


@pytest.fixture
def track(local_track):
    """The local track with a known digest floor, so the assertions do not
    depend on whichever profile is loaded."""
    t = dict(local_track)
    t["digest_min_fit"] = 0.4
    t["notify"] = False
    return t


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have been emailed. Returns the (subject, plain,
    html) list — empty means nothing was sent."""
    out = []

    def _fake(subject, plain, html):
        out.append((subject, plain, html))
        return True

    monkeypatch.setattr(digest_md, "send_gmail", _fake)
    return out


def row(job_id, fit=0.9, first_seen=TODAY, **over):
    r = {"job_id": job_id, "company_name": "Acme", "title": f"Role {job_id}",
         "url": f"https://acme.io/{job_id}", "location": "Anywhere, XX",
         "resume_fit_score": fit, "first_seen": first_seen,
         "fit_reason": "reason"}
    r.update(over)
    return r


class TestNewRankedRows:
    def test_keeps_only_rows_first_seen_since(self, track):
        ranked = [row("fresh"), row("stale", first_seen=YESTERDAY)]
        picked = [j["job_id"] for j in digest_md.new_ranked_rows(ranked, track)]
        assert picked == ["fresh"]

    def test_new_since_can_reach_back(self, track):
        ranked = [row("fresh"), row("stale", first_seen=YESTERDAY)]
        picked = [j["job_id"] for j in
                  digest_md.new_ranked_rows(ranked, track, new_since=YESTERDAY)]
        assert picked == ["fresh", "stale"]

    def test_drops_rows_under_the_floor(self, track):
        ranked = [row("good", fit=0.41), row("weak", fit=0.39)]
        picked = [j["job_id"] for j in digest_md.new_ranked_rows(ranked, track)]
        assert picked == ["good"]

    def test_unscored_rows_never_qualify(self, track):
        assert digest_md.new_ranked_rows([row("null", fit=None)], track) == []


class TestSendRankedDigest:
    def test_sends_only_the_new_rows(self, track, sent):
        ranked = [row("fresh"), row("stale", first_seen=YESTERDAY),
                  row("weak", fit=0.1)]
        assert digest_md.send_ranked_digest(ranked, track) is True
        subject, plain, html = sent[0]
        assert "1 new match(es)" in subject
        assert track["label"].upper() in subject
        assert "Role fresh" in plain and "Role fresh" in html
        assert "Role stale" not in plain
        assert "Role weak" not in plain

    def test_silent_when_nothing_is_new(self, track, sent, capsys):
        assert digest_md.send_ranked_digest(
            [row("stale", first_seen=YESTERDAY)], track) is False
        assert sent == []
        assert "skipping email" in capsys.readouterr().out

    def test_watch_hits_alone_still_send(self, track, sent):
        hits = [({"name": "Watched Co"},
                 {"title": "Any Role", "url": "https://w.co/1",
                  "location": "Anywhere"}, False)]
        assert digest_md.send_ranked_digest([], track, watch_hits=hits) is True
        _, plain, _ = sent[0]
        assert "Watched Co" in plain
        assert "0 new job(s)" in plain

    def test_pipeline_section_carries_only_fresh_closures(self, track, sent):
        pipeline = [
            {"disposition": "applied", "company_name": "Acme",
             "title": "Closed Today", "url": "https://acme.io/a",
             "status": "closed", "closed_at": TODAY},
            {"disposition": "applied", "company_name": "Acme",
             "title": "Closed Before", "url": "https://acme.io/b",
             "status": "closed", "closed_at": YESTERDAY},
            {"disposition": "saved", "company_name": "Acme",
             "title": "Still Open", "url": "https://acme.io/c",
             "status": "open", "closed_at": None},
        ]
        digest_md.send_ranked_digest([row("fresh")], track, pipeline=pipeline)
        _, plain, _ = sent[0]
        assert "Closed Today" in plain
        assert "Closed Before" not in plain
        assert "Still Open" not in plain

    def test_reports_failure_when_the_send_fails(self, track, monkeypatch):
        monkeypatch.setattr(digest_md, "send_gmail",
                            lambda *a, **k: False)
        assert digest_md.send_ranked_digest([row("fresh")], track) is False


class TestToast:
    def test_off_by_default(self, track):
        assert digest_md.toast(track, 3, "x.md") is False

    def test_no_toast_without_new_rows(self, track):
        assert digest_md.toast({**track, "notify": True}, 0, "x.md") is False

    def test_missing_package_degrades_silently(self, track, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def _blocked(name, *a, **k):
            if name.startswith("winotify"):
                raise ImportError("no winotify")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        assert digest_md.toast({**track, "notify": True}, 3, "x.md") is False


class TestTrackKeys:
    def test_engine_defaults_expose_the_digest_keys(self, cfg):
        for t in cfg.UI_TRACKS.values():
            assert isinstance(t["digest_min_fit"], float)
            assert isinstance(t["notify"], bool)

    def test_profile_can_override_the_floor(self, cfg):
        built = cfg._build_ui_tracks(
            {"x": {"engine": "local", "digest_min_fit": 0.75, "notify": True}})
        assert built["x"]["digest_min_fit"] == 0.75
        assert built["x"]["notify"] is True
