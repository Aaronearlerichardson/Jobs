"""Environment handling: src/config/secrets.py's Settings (reached as
`config.SETTINGS`).

A variable that EXISTS but is blank must read as unset. `os.environ.get`
doesn't do that -- it returns "" -- which made an exported-but-empty
`ANTHROPIC_API_KEY` (a CI runner, a shell profile clearing it) look like a
configured key: every `!= "YOUR_ANTHROPIC_API_KEY_HERE"` check flipped
true, so the scorers authenticated with nothing instead of falling back.
"""

import pathlib

import pytest

from src import config
from src.config.secrets import read_env


class TestEnvSettings:
    @pytest.mark.parametrize("value", ["", "   "])
    @pytest.mark.parametrize("name, default", [
        ("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY_HERE"),
        # An empty CLAUDE_MODEL would be sent to the API as the model id.
        ("CLAUDE_MODEL", "claude-sonnet-5")])
    def test_blank_is_unset(self, monkeypatch, name, default, value):
        monkeypatch.setenv(name, value)
        assert getattr(read_env(), name.lower()) == default

    def test_real_value_wins_and_is_trimmed(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-ant-xyz  ")
        monkeypatch.setenv("WEBUI_PORT", " 6000 ")
        env = read_env()
        assert (env.anthropic_api_key, env.webui_port) == ("sk-ant-xyz", 6000)

    def test_a_bad_value_names_the_variable_not_the_value(self, monkeypatch):
        monkeypatch.setenv("WEBUI_PORT", "not-a-port")
        monkeypatch.setenv("CLAUDE_CACHE_TTL", "forever")
        with pytest.raises(ValueError) as e:
            read_env()
        msg = str(e.value)
        assert "WEBUI_PORT" in msg and "CLAUDE_CACHE_TTL" in msg
        assert "not-a-port" not in msg

    def test_placeholder_means_unconfigured(self):
        """The 'is a key configured?' test used across api.py, webapp
        routes, and the server banner."""
        assert config.ANTHROPIC_API_KEY  # never blank: blank -> placeholder


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
        monkeypatch.setattr(paths.SETTINGS, "jobs_data_dir", None)
        (tmp_path / "data").mkdir()
        assert paths._resolve_data_dir(tmp_path) == tmp_path / "data"

    def test_no_data_dir_and_no_store_falls_through_to_the_per_user_dir(
            self, tmp_path, monkeypatch):
        from src.config import paths
        monkeypatch.setattr(paths.SETTINGS, "jobs_data_dir", None)
        assert paths._resolve_data_dir(tmp_path) == paths._platform_data_dir()
