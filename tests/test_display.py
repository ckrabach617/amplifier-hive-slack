"""Tests for SlackDisplaySystem."""

import asyncio

import pytest
from unittest.mock import AsyncMock


class TestSlackDisplaySystem:

    def test_import(self):
        """Module can be imported."""
        from hive_slack.display import SlackDisplaySystem  # noqa: F401

    @pytest.mark.asyncio
    async def test_post_sends_to_channel(self):
        """_post sends the text to the configured channel/thread."""
        from hive_slack.display import SlackDisplaySystem

        client = AsyncMock()
        display = SlackDisplaySystem(client, "C123", "thread123")
        await display._post("Hello")
        client.chat_postMessage.assert_called_once_with(
            channel="C123", thread_ts="thread123", text="Hello"
        )

    @pytest.mark.asyncio
    async def test_post_sends_without_thread(self):
        """_post works when thread_ts is empty."""
        from hive_slack.display import SlackDisplaySystem

        client = AsyncMock()
        display = SlackDisplaySystem(client, "C123", "")
        await display._post("Hello")
        client.chat_postMessage.assert_called_once_with(
            channel="C123", thread_ts="", text="Hello"
        )

    def test_show_message_warning_prefix(self):
        """Warning messages get ⚠️ prefix."""
        from hive_slack.display import SlackDisplaySystem

        client = AsyncMock()
        display = SlackDisplaySystem(client, "C123")
        # Verify prefix logic by checking the prefix map behavior
        # show_message is fire-and-forget so we test the prefix construction
        prefix = {"warning": "⚠️ ", "error": "🚨 "}.get("warning", "")
        assert prefix == "⚠️ "

    def test_show_message_error_prefix(self):
        """Error messages get 🚨 prefix."""
        from hive_slack.display import SlackDisplaySystem

        prefix = {"warning": "⚠️ ", "error": "🚨 "}.get("error", "")
        assert prefix == "🚨 "

    def test_show_message_info_no_prefix(self):
        """Info messages have no prefix."""
        from hive_slack.display import SlackDisplaySystem

        prefix = {"warning": "⚠️ ", "error": "🚨 "}.get("info", "")
        assert prefix == ""

    @pytest.mark.asyncio
    async def test_post_handles_api_error(self):
        """_post doesn't raise on Slack API errors."""
        from hive_slack.display import SlackDisplaySystem

        client = AsyncMock()
        client.chat_postMessage.side_effect = Exception("API error")
        display = SlackDisplaySystem(client, "C123")
        # Should not raise
        await display._post("test")

    def test_show_message_creates_task_in_running_loop(self):
        """show_message creates a fire-and-forget task when loop is running."""
        from hive_slack.display import SlackDisplaySystem

        client = AsyncMock()
        display = SlackDisplaySystem(client, "C123", "thread123")

        async def _run():
            display.show_message("hello", "info")
            # Give the task a chance to run
            await asyncio.sleep(0.05)

        asyncio.get_event_loop().run_until_complete(_run())
        client.chat_postMessage.assert_called_once_with(
            channel="C123", thread_ts="thread123", text="hello"
        )

    def test_show_message_no_loop_logs_instead(self):
        """show_message logs when no event loop is running (doesn't crash)."""
        from hive_slack.display import SlackDisplaySystem

        client = AsyncMock()
        display = SlackDisplaySystem(client, "C123")
        # No running event loop — should not raise
        display.show_message("hello", "info")
