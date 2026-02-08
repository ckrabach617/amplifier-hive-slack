"""Shared test fixtures for amplifier-hive-slack."""

import pytest

from hive_slack.config import (
    HiveSlackConfig,
    InstanceConfig,
    PersonaConfig,
    SlackConfig,
)


@pytest.fixture
def sample_config() -> HiveSlackConfig:
    """A fully populated test config (no env vars needed)."""
    return HiveSlackConfig(
        instance=InstanceConfig(
            name="alpha",
            bundle="foundation",
            working_dir="/tmp/test-workspace",
            persona=PersonaConfig(name="Alpha", emoji=":robot_face:"),
        ),
        slack=SlackConfig(
            app_token="xapp-test-token",
            bot_token="xoxb-test-token",
        ),
    )
