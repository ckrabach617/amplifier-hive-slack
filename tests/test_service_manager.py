"""Tests for service manager."""

from unittest.mock import MagicMock

from hive_slack.service_manager import (
    ServiceStatus,
    status,
)


class TestServiceStatus:
    """Test service status detection."""

    def test_not_installed_when_no_unit_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hive_slack.service_manager._unit_path",
            lambda: tmp_path / "nonexistent.service",
        )
        info = status()
        assert info.status == ServiceStatus.NOT_INSTALLED

    def test_running_status_parsed(self, tmp_path, monkeypatch):
        unit_file = tmp_path / "hive-slack.service"
        unit_file.write_text("[Service]\nExecStart=/bin/true\n")
        monkeypatch.setattr("hive_slack.service_manager._unit_path", lambda: unit_file)

        mock_result = MagicMock()
        mock_result.stdout = "ActiveState=active\nMainPID=12345\nSubState=running\n"
        monkeypatch.setattr(
            "hive_slack.service_manager._run_systemctl", lambda *a, **kw: mock_result
        )

        info = status()
        assert info.status == ServiceStatus.RUNNING
        assert info.pid == 12345

    def test_stopped_status_parsed(self, tmp_path, monkeypatch):
        unit_file = tmp_path / "hive-slack.service"
        unit_file.write_text("[Service]\nExecStart=/bin/true\n")
        monkeypatch.setattr("hive_slack.service_manager._unit_path", lambda: unit_file)

        mock_result = MagicMock()
        mock_result.stdout = "ActiveState=inactive\nMainPID=0\nSubState=dead\n"
        monkeypatch.setattr(
            "hive_slack.service_manager._run_systemctl", lambda *a, **kw: mock_result
        )

        info = status()
        assert info.status == ServiceStatus.STOPPED

    def test_failed_status_parsed(self, tmp_path, monkeypatch):
        unit_file = tmp_path / "hive-slack.service"
        unit_file.write_text("[Service]\nExecStart=/bin/true\n")
        monkeypatch.setattr("hive_slack.service_manager._unit_path", lambda: unit_file)

        mock_result = MagicMock()
        mock_result.stdout = "ActiveState=failed\nMainPID=0\nSubState=failed\n"
        monkeypatch.setattr(
            "hive_slack.service_manager._run_systemctl", lambda *a, **kw: mock_result
        )

        info = status()
        assert info.status == ServiceStatus.FAILED
