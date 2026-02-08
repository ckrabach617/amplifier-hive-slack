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
    """A fully populated multi-instance test config."""
    return HiveSlackConfig(
        instances={
            "alpha": InstanceConfig(
                name="alpha",
                bundle="foundation",
                working_dir="/tmp/test-alpha",
                persona=PersonaConfig(name="Alpha", emoji=":robot_face:"),
            ),
            "beta": InstanceConfig(
                name="beta",
                bundle="foundation",
                working_dir="/tmp/test-beta",
                persona=PersonaConfig(name="Beta", emoji=":gear:"),
            ),
        },
        default_instance="alpha",
        slack=SlackConfig(
            app_token="xapp-test-token",
            bot_token="xoxb-test-token",
        ),
    )
