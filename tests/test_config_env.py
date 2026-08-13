"""Environment handling in config.py.

A variable that EXISTS but is blank must read as unset. `os.environ.get`
doesn't do that — it returns "" — which made an exported-but-empty
`ANTHROPIC_API_KEY` (a CI runner, a shell profile clearing it) look like a
configured key: every `!= "YOUR_ANTHROPIC_API_KEY_HERE"` check flipped
true, so the scorers authenticated with nothing instead of falling back.
"""

import config


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
    """The 'is a key configured?' test used across claude.py, webapp
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
