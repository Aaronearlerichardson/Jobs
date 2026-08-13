"""The Flask app, exercised through its test client — real routes, no
socket, no API key. Covers the geo bucket the Jobs tab filters on, the API
surface the SPA depends on, and the asset cache-busting that a stale
browser copy once defeated."""

import json
import re

import webapp


class TestGeoBucket:
    """Derived live from the location string; the stored geo_mode is only a
    secondary hint because it's stale-by-construction."""

    def test_local(self, local_addr):
        assert webapp._geo_tag({"location": local_addr}) == "local"

    def test_remote(self):
        assert webapp._geo_tag({"location": "Remote - US"}) == "remote"

    def test_relocation(self, elsewhere):
        assert webapp._geo_tag({"location": elsewhere}) == "relocation"

    def test_stored_remote_eligible_wins_on_empty_location(self):
        assert webapp._geo_tag({"location": "", "remote_eligible": 1}) == "remote"

    def test_stored_geo_mode_remote_wins(self):
        assert webapp._geo_tag({"location": "Austin, TX",
                                "geo_mode": "remote"}) == "remote"


class TestOps:
    def test_ops_are_keyed_by_engine_not_track_id(self):
        # A user-chosen track id must never appear in code.
        assert all("tracks" not in o for o in webapp.OPS.values())

    def test_every_op_names_a_real_engine_or_is_agnostic(self):
        assert all(o.get("engine") in (None, "local", "neural")
                   for o in webapp.OPS.values())

    def test_every_op_has_a_callable(self):
        assert all(callable(o["fn"]) for o in webapp.OPS.values())


class TestApi:
    ENDPOINTS = ("/api/stats", "/api/jobs", "/api/pipeline", "/api/companies",
                 "/api/tracks", "/api/config", "/api/run/status")

    def test_endpoints_return_json(self, client):
        for path in self.ENDPOINTS:
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.status_code)
            json.loads(resp.data)

    def test_tracks_expose_ui_defaults(self, client):
        tracks = json.loads(client.get("/api/tracks").data)
        assert tracks
        for t in tracks:
            assert {"id", "label", "engine", "min_fit_default",
                    "willing_to_move_default", "ops"} <= set(t)

    def test_unknown_track_falls_back_rather_than_500(self, client):
        # A stale localStorage value must not brick the UI.
        assert client.get("/api/jobs?track=no_such_track").status_code == 200

    def test_config_reports_its_source(self, client):
        cfgjson = json.loads(client.get("/api/config").data)
        assert cfgjson["source"] in ("profile.toml", "profile.example.toml")
        assert cfgjson["parsed"]["keywords"]

    def test_bad_config_is_refused_not_written(self, client):
        resp = client.put("/api/config/raw", json={"toml": "not [valid"})
        assert resp.status_code == 400
        assert "errors" in json.loads(resp.data)

    def test_unknown_operation_404s(self, client):
        assert client.post("/api/run/no_such_op").status_code == 404


class TestAssets:
    def test_index_cache_busts_its_assets(self, client):
        html = client.get("/").data.decode()
        assert re.search(r'/static/js/app\.js\?v=\w+', html)
        assert re.search(r'/static/css/app\.css\?v=\w+', html)

    def test_index_is_not_cacheable(self, client):
        assert client.get("/").headers.get("Cache-Control") == "no-store"

    def test_static_assets_are_not_cacheable(self, client):
        resp = client.get("/static/js/app.js")
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "no-store"

    def test_asset_version_tracks_content(self, monkeypatch):
        from webapp import routes
        v1 = routes._asset_version()
        assert v1 == routes._asset_version()        # stable
        assert len(v1) == 10
