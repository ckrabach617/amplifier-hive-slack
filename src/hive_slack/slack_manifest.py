"""Slack App Manifest management.

Programmatically manage the Slack app's scopes, event subscriptions,
and other configuration via the App Manifest API.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"


def _get_config_token() -> str:
    """Get the Slack configuration token, rotating if needed."""
    token = os.environ.get("SLACK_CONFIG_TOKEN", "")
    if not token:
        raise RuntimeError(
            "SLACK_CONFIG_TOKEN not set. Generate one at "
            "https://api.slack.com/apps -> Your App Configuration Tokens"
        )
    return token


def _get_app_id() -> str:
    app_id = os.environ.get("SLACK_APP_ID", "")
    if not app_id:
        raise RuntimeError("SLACK_APP_ID not set in environment")
    return app_id


def _api_call(method: str, **kwargs: str) -> dict:
    """Make a Slack API call with the configuration token."""
    token = _get_config_token()
    resp = httpx.post(
        f"{SLACK_API_BASE}/{method}",
        headers={"Authorization": f"Bearer {token}"},
        data=kwargs,
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        error = data.get("error", "unknown")
        errors = data.get("errors", [])
        detail = f": {errors}" if errors else ""
        raise RuntimeError(f"Slack API {method} failed: {error}{detail}")
    return data


def export_manifest() -> dict:
    """Export the current app manifest from Slack."""
    data = _api_call("apps.manifest.export", app_id=_get_app_id())
    return data.get("manifest", {})


def validate_manifest(manifest: dict) -> tuple[bool, list[str]]:
    """Validate a manifest against Slack's schema. Returns (ok, errors)."""
    try:
        _api_call(
            "apps.manifest.validate",
            app_id=_get_app_id(),
            manifest=json.dumps(manifest),
        )
        return True, []
    except RuntimeError as e:
        return False, [str(e)]


def update_manifest(manifest: dict) -> dict:
    """Update the app's manifest. This is a FULL REPLACEMENT."""
    data = _api_call(
        "apps.manifest.update",
        app_id=_get_app_id(),
        manifest=json.dumps(manifest),
    )
    return data


def sync_from_file(manifest_path: str) -> dict:
    """Load a manifest from YAML file and push it to Slack."""
    path = Path(manifest_path)
    if not path.exists():
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")

    with open(path) as f:
        manifest = yaml.safe_load(f)

    # Validate first
    ok, errors = validate_manifest(manifest)
    if not ok:
        raise RuntimeError(f"Manifest validation failed: {errors}")

    return update_manifest(manifest)


def save_manifest(manifest: dict, path: str) -> None:
    """Save a manifest dict to a YAML file."""
    with open(path, "w") as f:
        yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)


def rotate_token() -> tuple[str, str]:
    """Rotate the configuration token. Returns (new_token, new_refresh_token)."""
    refresh_token = os.environ.get("SLACK_CONFIG_REFRESH_TOKEN", "")
    if not refresh_token:
        raise RuntimeError("SLACK_CONFIG_REFRESH_TOKEN not set")

    resp = httpx.post(
        f"{SLACK_API_BASE}/tooling.tokens.rotate",
        data={"refresh_token": refresh_token},
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Token rotation failed: {data.get('error', 'unknown')}")

    return data["token"], data["refresh_token"]


def get_reinstall_url() -> str:
    """Generate the OAuth reinstall URL (needed after scope changes)."""
    app_id = _get_app_id()
    # Get current scopes from manifest
    manifest = export_manifest()
    scopes = manifest.get("oauth_config", {}).get("scopes", {}).get("bot", [])
    scope_str = ",".join(scopes)  # noqa: F841

    # The client_id is different from app_id -- we need to look it up
    # For now, provide the manual reinstall URL
    return f"https://api.slack.com/apps/{app_id}/install-on-team"
