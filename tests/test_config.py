"""Tests for configuration loading."""

import pytest

from hive_slack.config import HiveSlackConfig


class TestConfigLoading:
    """Test YAML config loading and parsing (legacy single-instance format)."""

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

        assert config.instances["alpha"].name == "alpha"
        assert config.instances["alpha"].bundle == "foundation"
        assert config.instances["alpha"].working_dir == "/tmp/test"
        assert config.instances["alpha"].persona.name == "Alpha"
        assert config.instances["alpha"].persona.emoji == ":large_blue_diamond:"
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

        assert not config.instances["alpha"].working_dir.startswith("~")
        assert config.instances["alpha"].working_dir.endswith("/my-project")

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
        assert config.instances["alpha"].persona.emoji == ":robot_face:"


class TestMultiInstanceConfig:
    """Test multi-instance configuration."""

    def test_loads_multi_instance_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
instances:
  alpha:
    bundle: foundation
    working_dir: /tmp/alpha
    persona:
      name: Alpha
      emoji: ":robot_face:"
  beta:
    bundle: foundation
    working_dir: /tmp/beta
    persona:
      name: Beta
      emoji: ":gear:"
defaults:
  instance: alpha
slack:
  app_token: test-app
  bot_token: test-bot
""")
        config = HiveSlackConfig.from_yaml(str(config_file))

        assert len(config.instances) == 2
        assert "alpha" in config.instances
        assert "beta" in config.instances
        assert config.default_instance == "alpha"
        assert config.instances["alpha"].persona.name == "Alpha"
        assert config.instances["beta"].persona.emoji == ":gear:"

    def test_default_instance_defaults_to_first(self, tmp_path):
        """When no defaults section, first instance becomes default."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
instances:
  gamma:
    bundle: foundation
    working_dir: /tmp/gamma
slack:
  app_token: test
  bot_token: test
""")
        config = HiveSlackConfig.from_yaml(str(config_file))
        assert config.default_instance == "gamma"

    def test_get_instance_returns_correct_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
instances:
  alpha:
    bundle: foundation
    working_dir: /tmp/alpha
    persona:
      name: Alpha
      emoji: ":robot_face:"
  beta:
    bundle: custom-bundle
    working_dir: /tmp/beta
    persona:
      name: Beta
      emoji: ":gear:"
slack:
  app_token: test
  bot_token: test
""")
        config = HiveSlackConfig.from_yaml(str(config_file))

        alpha = config.get_instance("alpha")
        assert alpha.bundle == "foundation"

        beta = config.get_instance("beta")
        assert beta.bundle == "custom-bundle"

    def test_get_instance_unknown_raises_key_error(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
instances:
  alpha:
    bundle: foundation
    working_dir: /tmp/alpha
slack:
  app_token: test
  bot_token: test
""")
        config = HiveSlackConfig.from_yaml(str(config_file))

        with pytest.raises(KeyError, match="Unknown instance 'unknown'"):
            config.get_instance("unknown")

    def test_instance_names_property(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
instances:
  alpha:
    bundle: foundation
    working_dir: /tmp/alpha
  beta:
    bundle: foundation
    working_dir: /tmp/beta
slack:
  app_token: test
  bot_token: test
""")
        config = HiveSlackConfig.from_yaml(str(config_file))
        assert set(config.instance_names) == {"alpha", "beta"}

    def test_legacy_single_instance_still_works(self, tmp_path):
        """The old single-instance format should still parse correctly."""
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
  app_token: test
  bot_token: test
""")
        config = HiveSlackConfig.from_yaml(str(config_file))

        assert len(config.instances) == 1
        assert "alpha" in config.instances
        assert config.default_instance == "alpha"
        assert config.instances["alpha"].persona.name == "Alpha"
