"""Configuration loading for Amplifier Hive Slack connector."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml


@dataclass
class PersonaConfig:
    """How an instance appears in Slack."""

    name: str
    emoji: str = ":robot_face:"


@dataclass
class InstanceConfig:
    """Configuration for a single Amplifier instance."""

    name: str
    bundle: str
    working_dir: str
    persona: PersonaConfig


@dataclass
class SlackConfig:
    """Slack connection configuration."""

    app_token: str
    bot_token: str


@dataclass
class HiveSlackConfig:
    """Top-level configuration for the Hive Slack connector."""

    instance: InstanceConfig
    slack: SlackConfig

    @classmethod
    def from_yaml(cls, path: str) -> HiveSlackConfig:
        """Load configuration from a YAML file.

        Supports ${ENV_VAR} substitution in string values.
        Expands ~ in working_dir paths.
        """
        with open(path) as f:
            raw = yaml.safe_load(f)

        resolved = cast(dict[str, Any], _substitute_env_vars(raw))

        instance_data = cast(dict[str, Any], resolved["instance"])
        persona_data = cast(dict[str, Any], instance_data.get("persona", {}))

        working_dir = instance_data["working_dir"]
        if working_dir.startswith("~"):
            working_dir = str(Path(working_dir).expanduser())

        instance = InstanceConfig(
            name=instance_data["name"],
            bundle=instance_data["bundle"],
            working_dir=working_dir,
            persona=PersonaConfig(
                name=persona_data.get("name", instance_data["name"].title()),
                emoji=persona_data.get("emoji", ":robot_face:"),
            ),
        )

        slack_data = cast(dict[str, Any], resolved["slack"])
        slack = SlackConfig(
            app_token=slack_data["app_token"],
            bot_token=slack_data["bot_token"],
        )

        return cls(instance=instance, slack=slack)


_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _substitute_env_vars(data: Any) -> Any:
    """Recursively substitute ${ENV_VAR} references in string values."""
    if isinstance(data, str):

        def _replace(match):
            var_name = match.group(1)
            value = os.environ.get(var_name)
            if value is None:
                raise ValueError(
                    f"Environment variable '{var_name}' is not set "
                    f"(referenced as ${{{var_name}}} in config)"
                )
            return value

        return _ENV_VAR_PATTERN.sub(_replace, data)
    elif isinstance(data, dict):
        return {k: _substitute_env_vars(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_substitute_env_vars(item) for item in data]
    return data
