"""Tests for the setup wizard utilities."""

import json
import urllib.parse
from unittest.mock import patch

from hive_slack.setup import (
    SLACK_MANIFEST,
    _detect_wsl,
    _generate_manifest_url,
    _suggest_working_dir,
)


class TestManifestGeneration:
    """Test Slack manifest URL generation."""

    def test_manifest_url_contains_all_scopes(self):
        """The manifest URL includes all required scopes."""
        url = urllib.parse.unquote(_generate_manifest_url())
        assert "app_mentions:read" in url
        assert "chat:write" in url
        assert "files:read" in url
        assert "files:write" in url
        assert "reactions:read" in url

    def test_manifest_url_contains_all_events(self):
        """The manifest URL includes all required events."""
        url = urllib.parse.unquote(_generate_manifest_url())
        assert "app_mention" in url
        assert "message.channels" in url
        assert "message.im" in url
        assert "reaction_added" in url

    def test_manifest_url_enables_socket_mode(self):
        """The manifest has socket_mode_enabled."""
        assert SLACK_MANIFEST["settings"]["socket_mode_enabled"] is True

    def test_manifest_url_is_valid_url(self):
        """The generated URL is a valid HTTPS URL."""
        url = _generate_manifest_url()
        assert url.startswith("https://api.slack.com/apps?new_app=1&manifest_json=")

    def test_manifest_json_is_parseable(self):
        """The manifest portion of the URL is valid JSON when decoded."""
        url = _generate_manifest_url()
        json_part = url.split("manifest_json=")[1]
        decoded = urllib.parse.unquote(json_part)
        parsed = json.loads(decoded)
        assert parsed["display_information"]["name"] == "Amplifier"


class TestWSLDetection:
    """Test WSL and Windows path detection."""

    def test_detect_wsl_returns_bool(self):
        """_detect_wsl returns a boolean."""
        result = _detect_wsl()
        assert isinstance(result, bool)

    def test_suggest_working_dir_returns_string(self):
        """_suggest_working_dir returns a string path."""
        result = _suggest_working_dir()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_suggest_working_dir_ends_with_amplifier(self):
        """The suggested dir ends with 'Amplifier'."""
        result = _suggest_working_dir()
        assert result.endswith("Amplifier")

    @patch("hive_slack.setup._detect_wsl", return_value=True)
    @patch("hive_slack.setup._detect_windows_user", return_value="TestUser")
    @patch("pathlib.Path.exists", return_value=True)
    def test_wsl_suggests_windows_path(self, mock_exists, mock_user, mock_wsl):
        """On WSL with detected Windows user, suggests /mnt/c path."""
        result = _suggest_working_dir()
        assert "/mnt/c/Users/TestUser/Documents/Amplifier" == result

    @patch("hive_slack.setup._detect_wsl", return_value=False)
    def test_non_wsl_suggests_home_documents(self, mock_wsl):
        """On non-WSL, suggests ~/Documents/Amplifier."""
        result = _suggest_working_dir()
        assert result.endswith("Documents/Amplifier")
        assert "/mnt/c" not in result


class TestProviderDetection:
    """Test provider auto-detection with Gemini support."""

    def test_detects_gemini_from_google_api_key(self, monkeypatch):
        """GOOGLE_API_KEY triggers Gemini provider."""
        from hive_slack.service import InProcessSessionManager

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

        provider = InProcessSessionManager._detect_provider()
        assert provider is not None
        assert provider["module"] == "provider-gemini"

    def test_detects_gemini_from_gemini_api_key(self, monkeypatch):
        """GEMINI_API_KEY also triggers Gemini provider."""
        from hive_slack.service import InProcessSessionManager

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        provider = InProcessSessionManager._detect_provider()
        assert provider is not None
        assert provider["module"] == "provider-gemini"

    def test_anthropic_takes_priority_over_gemini(self, monkeypatch):
        """If both Anthropic and Gemini keys exist, Anthropic wins."""
        from hive_slack.service import InProcessSessionManager

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

        provider = InProcessSessionManager._detect_provider()
        assert provider["module"] == "provider-anthropic"
