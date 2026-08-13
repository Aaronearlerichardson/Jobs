"""profile.toml editing (core/profile_edit.py) — validation, comment-
preserving updates, and the backup-then-atomic-write the Settings tab
relies on. Writes are redirected to tmp_path; the real profile is never
touched."""

import tomllib

import pytest

from core import profile_edit


@pytest.fixture
def raw():
    text, _ = profile_edit.read_raw()
    return text


@pytest.fixture
def sandbox(tmp_path, monkeypatch, raw):
    """A throwaway copy of the profile that write tests may clobber."""
    import config
    target = tmp_path / "profile.toml"
    target.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(config, "PROFILE_PATH", target)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return target


class TestRead:
    def test_finds_a_profile(self):
        text, source = profile_edit.read_raw()
        assert text and source in ("profile.toml", "profile.example.toml")

    def test_the_active_profile_is_valid(self, raw):
        assert profile_edit.validate(raw) == []


class TestValidate:
    def test_rejects_bad_toml(self):
        errs = profile_edit.validate("not [valid")
        assert any("syntax" in e for e in errs)

    def test_requires_core_sections(self):
        errs = profile_edit.validate("x = 1")
        assert any("keywords" in e for e in errs)

    def test_rejects_out_of_range_weight(self):
        errs = profile_edit.validate(
            "[keywords]\n[locations]\n[locality]\n[fit]\nweights = {domain = 1.5}")
        assert any("0..1" in e for e in errs)

    def test_rejects_non_string_keyword_list(self):
        errs = profile_edit.validate(
            "[locations]\n[locality]\n[keywords]\ncore = [1, 2]")
        assert any("list of strings" in e for e in errs)

    def test_allows_arrays_of_tables(self):
        # discovery.priority_companies is [{name=..,ats=..,slug=..}, ...]
        errs = profile_edit.validate(
            '[keywords]\n[locations]\n[locality]\n[discovery]\n'
            'priority_companies = [{name="A", ats="greenhouse", slug="a"}]')
        assert errs == []

    def test_requires_a_track_db(self):
        errs = profile_edit.validate(
            '[keywords]\n[locations]\n[locality]\n[tracks.x]\ndb = ""')
        assert any("db" in e for e in errs)


class TestApplyUpdates:
    def test_sets_scalar_and_list_values(self, raw):
        text = profile_edit.apply_updates({"keywords.core": ["a", "b"],
                                           "fit.weights.domain": 0.31})
        parsed = tomllib.loads(text)
        assert parsed["keywords"]["core"] == ["a", "b"]
        assert parsed["fit"]["weights"]["domain"] == 0.31

    def test_leaves_untouched_sections_intact(self, raw):
        before = tomllib.loads(raw)
        after = tomllib.loads(profile_edit.apply_updates(
            {"keywords.core": ["a"]}))
        assert all(after[k] == before[k] for k in before if k != "keywords")

    def test_preserves_comments_outside_edited_values(self, raw):
        text = profile_edit.apply_updates({"keywords.core": ["a"]})
        banners = [ln for ln in raw.splitlines() if ln.startswith("# ---")]
        assert banners and all(ln in text for ln in banners)

    def test_creates_missing_intermediate_tables(self):
        text = profile_edit.apply_updates({"tracks.brand_new.min_fit_default": 0.4})
        assert tomllib.loads(text)["tracks"]["brand_new"]["min_fit_default"] == 0.4

    def test_output_still_validates(self):
        text = profile_edit.apply_updates({"keywords.core": ["a"]})
        assert profile_edit.validate(text) == []


class TestBackupThenWrite:
    def test_writes_and_backs_up(self, sandbox):
        original = sandbox.read_text(encoding="utf-8")
        new_text = profile_edit.apply_updates({"keywords.core": ["sentinel"]})
        backup = profile_edit.backup_then_write(new_text)
        assert "sentinel" in sandbox.read_text(encoding="utf-8")
        assert backup and backup.exists()
        assert backup.read_text(encoding="utf-8") == original

    def test_backups_are_pruned(self, sandbox):
        for i in range(profile_edit.BACKUP_KEEP + 3):
            profile_edit.backup_then_write(
                profile_edit.apply_updates({"keywords.core": [f"v{i}"]}))
        kept = list((sandbox.parent / profile_edit.BACKUP_DIR_NAME)
                    .glob("profile-*.toml"))
        assert len(kept) <= profile_edit.BACKUP_KEEP

    def test_no_tmp_file_left_behind(self, sandbox):
        profile_edit.backup_then_write(
            profile_edit.apply_updates({"keywords.core": ["x"]}))
        assert not list(sandbox.parent.glob("*.tmp"))
