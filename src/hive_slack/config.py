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

    instances: dict[str, InstanceConfig]
    default_instance: str
    slack: SlackConfig

    def get_instance(self, name: str) -> InstanceConfig:
        """Get instance config by name. Raises KeyError if not found."""
        if name not in self.instances:
            available = ", ".join(sorted(self.instances.keys()))
            raise KeyError(f"Unknown instance '{name}'. Available: {available}")
        return self.instances[name]

    @property
    def instance_names(self) -> list[str]:
        """List of all registered instance names."""
        return list(self.instances.keys())

    @classmethod
    def from_yaml(cls, path: str) -> HiveSlackConfig:
        """Load configuration from a YAML file.

        Supports two formats:
        - Multi-instance (preferred): instances: {alpha: {...}, beta: {...}}
        - Single-instance (legacy): instance: {name: alpha, ...}

        Supports ${ENV_VAR} substitution in string values.
        Expands ~ in working_dir paths.
        """
        with open(path) as f:
            raw = yaml.safe_load(f)

        resolved = cast(dict[str, Any], _substitute_env_vars(raw))

        # Parse instances — support both multi and single format
        instances: dict[str, InstanceConfig] = {}
        default_instance: str = ""

        if "instances" in resolved:
            # Multi-instance format
            instances_data = cast(dict[str, Any], resolved["instances"])
            for inst_name, inst_data in instances_data.items():
                instances[inst_name] = _parse_instance(inst_name, inst_data)
            # Default from config or first instance
            defaults = cast(dict[str, Any], resolved.get("defaults", {}))
            default_instance = defaults.get("instance", next(iter(instances)))
        elif "instance" in resolved:
            # Legacy single-instance format
            inst_data = cast(dict[str, Any], resolved["instance"])
            inst_name = inst_data["name"]
            instances[inst_name] = _parse_instance(inst_name, inst_data)
            default_instance = inst_name

        if not instances:
            raise ValueError("Config must define at least one instance")

        slack_data = cast(dict[str, Any], resolved["slack"])
        slack = SlackConfig(
            app_token=slack_data["app_token"],
            bot_token=slack_data["bot_token"],
        )

        return cls(
            instances=instances,
            default_instance=default_instance,
            slack=slack,
        )


def _parse_instance(name: str, data: dict[str, Any]) -> InstanceConfig:
    """Parse a single instance config from dict."""
    persona_data = cast(dict[str, Any], data.get("persona", {}))

    working_dir = data.get("working_dir", f"~/amplifier-working-{name}")
    if working_dir.startswith("~"):
        working_dir = str(Path(working_dir).expanduser())

    return InstanceConfig(
        name=name,
        bundle=data.get("bundle", "foundation"),
        working_dir=working_dir,
        persona=PersonaConfig(
            name=persona_data.get("name", name.title()),
            emoji=persona_data.get("emoji", ":robot_face:"),
        ),
    )


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
