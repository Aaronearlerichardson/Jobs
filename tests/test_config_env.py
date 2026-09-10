"""Environment handling in src/config/secrets.py (reached as `config.env`).

A variable that EXISTS but is blank must read as unset. `os.environ.get`
doesn't do that — it returns "" — which made an exported-but-empty
`ANTHROPIC_API_KEY` (a CI runner, a shell profile clearing it) look like a
configured key: every `!= "YOUR_ANTHROPIC_API_KEY_HERE"` check flipped
true, so the scorers authenticated with nothing instead of falling back.
"""

import pathlib

from src import config


class TestEnvHelper:
    def test_missing_returns_default(self, monkeypatch):
        monkeypatch.delenv("SOME_UNSET_VAR", raising=False)
        assert config.env("SOME_UNSET_VAR", "fallback") == "fallback"

    def test_empty_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("SOME_VAR", "")
        assert config.env("SOME_VAR", "fallback") == "fallback"

    def test_whitespace_is_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("SOME_VAR", "   ")
        assert config.env("SOME_VAR", "fallback") == "fallback"

    def test_real_value_wins_and_is_trimmed(self, monkeypatch):
        monkeypatch.setenv("SOME_VAR", "  sk-ant-xyz  ")
        assert config.env("SOME_VAR", "fallback") == "sk-ant-xyz"

    def test_no_default_yields_empty_string(self, monkeypatch):
        monkeypatch.delenv("SOME_UNSET_VAR", raising=False)
        assert config.env("SOME_UNSET_VAR") == ""


class TestKeyDetection:
    """The 'is a key configured?' test used across api.py, webapp
    routes, and the server banner."""

    def test_placeholder_means_unconfigured(self):
        assert config.ANTHROPIC_API_KEY  # never blank: blank -> placeholder

    def test_blank_env_resolves_to_the_placeholder(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        assert (config.env("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY_HERE")
                == "YOUR_ANTHROPIC_API_KEY_HERE")

    def test_model_names_never_resolve_blank(self, monkeypatch):
        # An empty CLAUDE_MODEL would be sent to the API as the model id.
        monkeypatch.setenv("CLAUDE_MODEL", "")
        assert config.env("CLAUDE_MODEL", "claude-sonnet-5") == "claude-sonnet-5"


class TestCodeRoot:
    """SCRIPT_DIR is where the CODE lives, and every other path hangs off
    it: APP_HOME, then DATA_DIR (the store, the profile, the reports).

    It is derived by walking up from `src/config/paths.py`, so moving the
    package tree changes how far up "the root" is. When the tree moved
    under src/ the walk was left at two levels and pointed at src/ --
    DATA_DIR silently fell through to the empty per-user default, and the
    app came up working, on nothing: 887 companies became 0, and the
    harvester planned no boards at all. Nothing in the suite noticed.
    """

    def test_script_dir_is_the_checkout_root(self):
        from src.config import paths
        root = pathlib.Path(paths.__file__).resolve().parents[2]
        assert paths.SCRIPT_DIR == root
        assert (paths.SCRIPT_DIR / "src" / "config" / "paths.py").is_file(), \
            f"SCRIPT_DIR {paths.SCRIPT_DIR} is not the code root"

    def test_the_root_holds_the_entry_points(self):
        from src.config import paths
        for entry in ("run_scraper.py", "webapp.py", "harvest.py"):
            assert (paths.SCRIPT_DIR / entry).is_file(), \
                f"{entry} missing from SCRIPT_DIR {paths.SCRIPT_DIR}"

    def test_an_in_checkout_data_dir_wins_over_the_per_user_default(
            self, tmp_path, monkeypatch):
        from src.config import paths
        monkeypatch.delenv("JOBS_DATA_DIR", raising=False)
        (tmp_path / "data").mkdir()
        assert paths._resolve_data_dir(tmp_path) == tmp_path / "data"

    def test_no_data_dir_and_no_store_falls_through_to_the_per_user_dir(
            self, tmp_path, monkeypatch):
        from src.config import paths
        monkeypatch.delenv("JOBS_DATA_DIR", raising=False)
        assert paths._resolve_data_dir(tmp_path) == paths._platform_data_dir()
