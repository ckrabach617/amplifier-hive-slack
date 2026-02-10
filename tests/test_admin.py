"""Tests for the admin UI components."""

import os
import time

import pytest


class TestAdminHelpers:
    """Test admin UI helper functions."""

    def test_format_uptime_seconds(self):
        from hive_slack.admin.shared import format_uptime
        now = time.time()
        assert format_uptime(now - 30) == "30s"

    def test_format_uptime_minutes(self):
        from hive_slack.admin.shared import format_uptime
        now = time.time()
        result = format_uptime(now - 150)
        assert "2m" in result

    def test_format_uptime_hours(self):
        from hive_slack.admin.shared import format_uptime
        now = time.time()
        result = format_uptime(now - 7500)
        assert "2h" in result

    def test_mask_key_short(self):
        from hive_slack.admin.configuration import _mask_key
        assert _mask_key("short") == "****"

    def test_mask_key_normal(self):
        from hive_slack.admin.configuration import _mask_key
        result = _mask_key("sk-ant-1234567890abcdefghij")
        assert result.startswith("sk-ant-1")
        assert result.endswith("ghij")
        assert "..." in result

    def test_mask_key_empty(self):
        from hive_slack.admin.configuration import _mask_key
        assert _mask_key("") == "****"

    def test_format_size_bytes(self):
        from hive_slack.admin.configuration import _format_size
        assert _format_size(500) == "500 B"

    def test_format_size_kb(self):
        from hive_slack.admin.configuration import _format_size
        result = _format_size(2048)
        assert "KB" in result

    def test_format_size_mb(self):
        from hive_slack.admin.configuration import _format_size
        result = _format_size(2 * 1024 * 1024)
        assert "MB" in result

    def test_detect_provider_anthropic(self, monkeypatch):
        from hive_slack.admin.configuration import _detect_provider_info
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test12345678901234")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        name, key = _detect_provider_info()
        assert "Anthropic" in name
        assert "****" not in key or "..." in key  # masked but not fully hidden

    def test_detect_provider_none(self, monkeypatch):
        from hive_slack.admin.configuration import _detect_provider_info
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        name, key = _detect_provider_info()
        assert "None" in name

    def test_nicegui_available(self):
        """NiceGUI should be available since we installed it."""
        from hive_slack.main import _nicegui_available
        assert _nicegui_available() is True


class TestLogBuffer:
    """Test the ring buffer log handler."""

    def test_log_records_captured(self):
        import logging
        from hive_slack.admin.logs import _log_buffer

        initial = len(_log_buffer)
        logger = logging.getLogger("test.admin.buffer")
        logger.warning("test warning message")
        assert len(_log_buffer) > initial

    def test_error_capture(self):
        import logging
        from hive_slack.admin.dashboard import _recent_errors

        initial = len(_recent_errors)
        logger = logging.getLogger("test.admin.errors")
        logger.warning("test error for dashboard")
        assert len(_recent_errors) > initial


class TestCreateAdminApp:
    """Test admin app initialization."""

    def test_create_admin_app_sets_globals(self):
        from unittest.mock import MagicMock
        import hive_slack.admin as admin_state
        from hive_slack.admin import create_admin_app

        mock_service = MagicMock()
        mock_connector = MagicMock()
        mock_config = MagicMock()

        create_admin_app(mock_service, mock_connector, mock_config)

        assert admin_state._service is mock_service
        assert admin_state._connector is mock_connector
        assert admin_state._config is mock_config
        assert admin_state._start_time > 0
