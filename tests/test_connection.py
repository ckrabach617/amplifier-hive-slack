"""Tests for SlackConnection tracking fields."""

import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from hive_slack.config import (
    HiveSlackConfig,
    InstanceConfig,
    PersonaConfig,
    SlackConfig,
)
from hive_slack.connection import SlackConnection


def make_config() -> HiveSlackConfig:
    return HiveSlackConfig(
        instances={
            "alpha": InstanceConfig(
                name="alpha",
                bundle="foundation",
                working_dir="/tmp/test",
                persona=PersonaConfig(name="Alpha", emoji=":robot_face:"),
            ),
        },
        default_instance="alpha",
        slack=SlackConfig(app_token="xapp-test", bot_token="xoxb-test"),
    )


class TestConnectionTrackingFields:
    """Test tracking fields on SlackConnection."""

    def test_initial_values(self):
        """New connection has None timestamps and zero reconnect count."""
        app = MagicMock()
        config = make_config()
        with patch("hive_slack.connection.AsyncSocketModeHandler"):
            conn = SlackConnection(app, config)

        assert conn.started_at is None
        assert conn.last_health_check_at is None
        assert conn.reconnect_count == 0

    @pytest.mark.asyncio
    async def test_start_sets_started_at(self):
        """start() records the connection start time."""
        app = MagicMock()
        app.client.auth_test = AsyncMock(return_value={"user_id": "U123"})
        config = make_config()
        with patch("hive_slack.connection.AsyncSocketModeHandler") as MockHandler:
            handler = AsyncMock()
            MockHandler.return_value = handler
            conn = SlackConnection(app, config)

        before = time.monotonic()
        await conn.start()
        after = time.monotonic()

        assert conn.started_at is not None
        assert before <= conn.started_at <= after

    @pytest.mark.asyncio
    async def test_reconnect_increments_count(self):
        """Each reconnect() call increments the reconnect counter."""
        app = MagicMock()
        config = make_config()
        with patch("hive_slack.connection.AsyncSocketModeHandler") as MockHandler:
            MockHandler.return_value = AsyncMock()
            conn = SlackConnection(app, config)

            assert conn.reconnect_count == 0
            await conn.reconnect()
            assert conn.reconnect_count == 1
            await conn.reconnect()
            assert conn.reconnect_count == 2

    @pytest.mark.asyncio
    async def test_health_check_updates_timestamp(self):
        """Successful health check in watchdog updates last_health_check_at."""
        app = MagicMock()
        app.client.auth_test = AsyncMock(return_value={"ok": True})
        config = make_config()
        with patch("hive_slack.connection.AsyncSocketModeHandler") as MockHandler:
            MockHandler.return_value = AsyncMock()
            conn = SlackConnection(app, config)

            assert conn.last_health_check_at is None

            iteration = 0

            async def fake_sleep(_interval):
                nonlocal iteration
                iteration += 1
                if iteration > 8:
                    raise asyncio.CancelledError

            with patch("asyncio.sleep", side_effect=fake_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await conn.run_watchdog(interval=15.0)

            assert conn.last_health_check_at is not None

    @pytest.mark.asyncio
    async def test_failed_health_check_does_not_update_timestamp(self):
        """Failed health check leaves last_health_check_at unchanged."""
        app = MagicMock()
        app.client.auth_test = AsyncMock(side_effect=RuntimeError("timeout"))
        config = make_config()
        with patch("hive_slack.connection.AsyncSocketModeHandler") as MockHandler:
            MockHandler.return_value = AsyncMock()
            conn = SlackConnection(app, config)

            iteration = 0

            async def fake_sleep(_interval):
                nonlocal iteration
                iteration += 1
                if iteration > 8:
                    raise asyncio.CancelledError

            with patch("asyncio.sleep", side_effect=fake_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await conn.run_watchdog(interval=15.0)

            assert conn.last_health_check_at is None
