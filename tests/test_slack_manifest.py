"""Tests for Slack manifest management."""

from unittest.mock import patch, MagicMock

import pytest

from hive_slack.slack_manifest import (
    export_manifest,
    validate_manifest,
    save_manifest,
    _get_app_id,
    _get_config_token,
)


class TestManifestExport:
    def test_export_returns_manifest(self, monkeypatch):
        monkeypatch.setenv("SLACK_CONFIG_TOKEN", "xoxe.xoxp-test")
        monkeypatch.setenv("SLACK_APP_ID", "A12345")

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "manifest": {
                "display_information": {"name": "Test App"},
                "oauth_config": {"scopes": {"bot": ["chat:write"]}},
            },
        }

        with patch("hive_slack.slack_manifest.httpx.post", return_value=mock_response):
            manifest = export_manifest()

        assert manifest["display_information"]["name"] == "Test App"
        assert "chat:write" in manifest["oauth_config"]["scopes"]["bot"]

    def test_export_raises_on_error(self, monkeypatch):
        monkeypatch.setenv("SLACK_CONFIG_TOKEN", "xoxe.xoxp-test")
        monkeypatch.setenv("SLACK_APP_ID", "A12345")

        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False, "error": "invalid_auth"}

        with patch("hive_slack.slack_manifest.httpx.post", return_value=mock_response):
            with pytest.raises(RuntimeError, match="invalid_auth"):
                export_manifest()


class TestManifestSave:
    def test_saves_yaml_file(self, tmp_path):
        manifest = {"display_information": {"name": "Test"}}
        path = tmp_path / "manifest.yaml"
        save_manifest(manifest, str(path))

        assert path.exists()
        content = path.read_text()
        assert "name: Test" in content


class TestMissingCredentials:
    def test_missing_app_id_raises(self, monkeypatch):
        monkeypatch.delenv("SLACK_APP_ID", raising=False)
        with pytest.raises(RuntimeError, match="SLACK_APP_ID"):
            _get_app_id()

    def test_missing_config_token_raises(self, monkeypatch):
        monkeypatch.delenv("SLACK_CONFIG_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="SLACK_CONFIG_TOKEN"):
            _get_config_token()
