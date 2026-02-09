"""Tests for connector-provided Slack tools."""

import pytest
from unittest.mock import AsyncMock


class TestSlackSendMessageTool:

    def test_import(self):
        """Module can be imported."""
        from hive_slack.tools import SlackSendMessageTool  # noqa: F401

    def test_name(self):
        from hive_slack.tools import SlackSendMessageTool

        tool = SlackSendMessageTool(AsyncMock(), "C123")
        assert tool.name == "slack_send_message"

    def test_description(self):
        from hive_slack.tools import SlackSendMessageTool

        tool = SlackSendMessageTool(AsyncMock(), "C123")
        assert tool.description  # Non-empty

    def test_has_input_schema(self):
        from hive_slack.tools import SlackSendMessageTool

        tool = SlackSendMessageTool(AsyncMock(), "C123")
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert "text" in schema["properties"]
        assert "text" in schema["required"]

    @pytest.mark.asyncio
    async def test_execute_sends_message(self):
        from hive_slack.tools import SlackSendMessageTool

        client = AsyncMock()
        client.chat_postMessage.return_value = {"ok": True}
        tool = SlackSendMessageTool(client, "C123", "thread123")

        result = await tool.execute({"text": "Hello!"})

        assert result.success is True
        client.chat_postMessage.assert_called_once()
        call_kwargs = client.chat_postMessage.call_args[1]
        assert call_kwargs["text"] == "Hello!"
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["thread_ts"] == "thread123"

    @pytest.mark.asyncio
    async def test_execute_custom_channel(self):
        from hive_slack.tools import SlackSendMessageTool

        client = AsyncMock()
        client.chat_postMessage.return_value = {"ok": True}
        tool = SlackSendMessageTool(client, "C123", "thread123")

        result = await tool.execute({"text": "Hello!", "channel": "C999"})

        assert result.success is True
        call_kwargs = client.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C999"

    @pytest.mark.asyncio
    async def test_execute_empty_text_fails(self):
        from hive_slack.tools import SlackSendMessageTool

        tool = SlackSendMessageTool(AsyncMock(), "C123")
        result = await tool.execute({"text": ""})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_handles_api_error(self):
        from hive_slack.tools import SlackSendMessageTool

        client = AsyncMock()
        client.chat_postMessage.side_effect = Exception("rate limited")
        tool = SlackSendMessageTool(client, "C123")
        result = await tool.execute({"text": "test"})
        assert result.success is False
        assert "rate limited" in result.output


class TestSlackReactionTool:

    def test_name(self):
        from hive_slack.tools import SlackReactionTool

        tool = SlackReactionTool(AsyncMock(), "C123")
        assert tool.name == "slack_add_reaction"

    def test_description(self):
        from hive_slack.tools import SlackReactionTool

        tool = SlackReactionTool(AsyncMock(), "C123")
        assert tool.description

    def test_has_input_schema(self):
        from hive_slack.tools import SlackReactionTool

        tool = SlackReactionTool(AsyncMock(), "C123")
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert "emoji" in schema["properties"]
        assert "emoji" in schema["required"]

    @pytest.mark.asyncio
    async def test_execute_adds_reaction(self):
        from hive_slack.tools import SlackReactionTool

        client = AsyncMock()
        tool = SlackReactionTool(client, "C123", "user_msg_ts")

        result = await tool.execute({"emoji": "thumbsup"})

        assert result.success is True
        client.reactions_add.assert_called_once_with(
            channel="C123", name="thumbsup", timestamp="user_msg_ts"
        )

    @pytest.mark.asyncio
    async def test_execute_custom_timestamp(self):
        from hive_slack.tools import SlackReactionTool

        client = AsyncMock()
        tool = SlackReactionTool(client, "C123", "default_ts")

        result = await tool.execute({"emoji": "fire", "message_ts": "custom_ts"})

        assert result.success is True
        client.reactions_add.assert_called_once_with(
            channel="C123", name="fire", timestamp="custom_ts"
        )

    @pytest.mark.asyncio
    async def test_execute_no_emoji_fails(self):
        from hive_slack.tools import SlackReactionTool

        tool = SlackReactionTool(AsyncMock(), "C123", "ts")
        result = await tool.execute({"emoji": ""})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_no_timestamp_fails(self):
        from hive_slack.tools import SlackReactionTool

        tool = SlackReactionTool(AsyncMock(), "C123", "")
        result = await tool.execute({"emoji": "thumbsup"})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_handles_api_error(self):
        from hive_slack.tools import SlackReactionTool

        client = AsyncMock()
        client.reactions_add.side_effect = Exception("already_reacted")
        tool = SlackReactionTool(client, "C123", "ts123")
        result = await tool.execute({"emoji": "thumbsup"})
        assert result.success is False
        assert "already_reacted" in result.output


class TestCreateSlackTools:

    def test_creates_two_tools(self):
        from hive_slack.tools import create_slack_tools

        tools = create_slack_tools(AsyncMock(), "C123")
        assert len(tools) == 2

    def test_tool_names(self):
        from hive_slack.tools import create_slack_tools

        tools = create_slack_tools(AsyncMock(), "C123")
        names = {t.name for t in tools}
        assert "slack_send_message" in names
        assert "slack_add_reaction" in names

    def test_passes_thread_and_user_ts(self):
        from hive_slack.tools import create_slack_tools

        tools = create_slack_tools(AsyncMock(), "C123", "thread_ts", "user_ts")
        # Verify the reaction tool got the user_ts
        reaction_tool = next(t for t in tools if t.name == "slack_add_reaction")
        assert reaction_tool._last_user_ts == "user_ts"
        # Verify the send tool got the thread_ts
        send_tool = next(t for t in tools if t.name == "slack_send_message")
        assert send_tool._default_thread_ts == "thread_ts"
