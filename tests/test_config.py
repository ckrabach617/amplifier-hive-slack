"""Tests for configuration loading."""

import pytest

from hive_slack.config import HiveSlackConfig


class TestConfigLoading:
    """Test YAML config loading and parsing."""

    def test_loads_basic_config(self, tmp_path):
        """Config loads all fields from a well-formed YAML file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
instance:
  name: alpha
  bundle: foundation
  working_dir: /tmp/test
  persona:
    name: Alpha
    emoji: ":large_blue_diamond:"
slack:
  app_token: test-app-token
  bot_token: test-bot-token
""")
        config = HiveSlackConfig.from_yaml(str(config_file))

        assert config.instance.name == "alpha"
        assert config.instance.bundle == "foundation"
        assert config.instance.working_dir == "/tmp/test"
        assert config.instance.persona.name == "Alpha"
        assert config.instance.persona.emoji == ":large_blue_diamond:"
        assert config.slack.app_token == "test-app-token"
        assert config.slack.bot_token == "test-bot-token"

    def test_substitutes_env_vars(self, tmp_path, monkeypatch):
        """${ENV_VAR} references are replaced with environment variable values."""
        monkeypatch.setenv("TEST_APP_TOKEN", "xapp-secret")
        monkeypatch.setenv("TEST_BOT_TOKEN", "xoxb-secret")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
instance:
  name: alpha
  bundle: foundation
  working_dir: /tmp/test
  persona:
    name: Alpha
    emoji: ":robot_face:"
slack:
  app_token: ${TEST_APP_TOKEN}
  bot_token: ${TEST_BOT_TOKEN}
""")
        config = HiveSlackConfig.from_yaml(str(config_file))

        assert config.slack.app_token == "xapp-secret"
        assert config.slack.bot_token == "xoxb-secret"

    def test_missing_env_var_raises_error(self, tmp_path):
        """Referencing an unset env var produces a clear error message."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
instance:
  name: alpha
  bundle: foundation
  working_dir: /tmp/test
  persona:
    name: Alpha
    emoji: ":robot_face:"
slack:
  app_token: ${DEFINITELY_NOT_SET_12345}
  bot_token: literal
""")
        with pytest.raises(ValueError, match="DEFINITELY_NOT_SET_12345"):
            HiveSlackConfig.from_yaml(str(config_file))

    def test_expands_tilde_in_working_dir(self, tmp_path):
        """~ in working_dir is expanded to the user's home directory."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
instance:
  name: alpha
  bundle: foundation
  working_dir: ~/my-project
  persona:
    name: Alpha
    emoji: ":robot_face:"
slack:
  app_token: test
  bot_token: test
""")
        config = HiveSlackConfig.from_yaml(str(config_file))

        assert not config.instance.working_dir.startswith("~")
        assert config.instance.working_dir.endswith("/my-project")

    def test_default_persona_emoji(self, tmp_path):
        """Persona emoji defaults to :robot_face: if not specified."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
instance:
  name: alpha
  bundle: foundation
  working_dir: /tmp/test
  persona:
    name: Alpha
slack:
  app_token: test
  bot_token: test
""")
        config = HiveSlackConfig.from_yaml(str(config_file))
        assert config.instance.persona.emoji == ":robot_face:"
