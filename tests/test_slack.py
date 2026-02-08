"""Tests for SlackConnector."""

import time

import pytest
from unittest.mock import AsyncMock

from hive_slack.config import HiveSlackConfig, InstanceConfig, PersonaConfig, SlackConfig
from hive_slack.slack import ChannelConfig, SlackConnector


def make_config() -> HiveSlackConfig:
    return HiveSlackConfig(
        instances={
            "alpha": InstanceConfig(
                name="alpha",
                bundle="foundation",
                working_dir="/tmp/test",
                persona=PersonaConfig(name="Alpha", emoji=":robot_face:"),
            ),
            "beta": InstanceConfig(
                name="beta",
                bundle="foundation",
                working_dir="/tmp/test-beta",
                persona=PersonaConfig(name="Beta", emoji=":gear:"),
            ),
        },
        default_instance="alpha",
        slack=SlackConfig(
            app_token="xapp-test",
            bot_token="xoxb-test",
        ),
    )


class TestStripMention:
    """Test mention stripping from message text."""

    def test_strips_single_mention(self):
        assert SlackConnector._strip_mention("<@U12345> hello") == "hello"

    def test_strips_mention_with_extra_spaces(self):
        assert SlackConnector._strip_mention("<@U12345>   hello world") == "hello world"

    def test_strips_mention_at_start_only(self):
        """Mentions in the middle of text are preserved (they're references, not the bot)."""
        result = SlackConnector._strip_mention("<@UBOT> ask <@UHUMAN> about this")
        assert result == "ask <@UHUMAN> about this"

    def test_handles_no_mention(self):
        assert SlackConnector._strip_mention("hello world") == "hello world"

    def test_handles_empty_string(self):
        assert SlackConnector._strip_mention("") == ""

    def test_handles_mention_only(self):
        """If the message is just a mention with no text, returns empty."""
        assert SlackConnector._strip_mention("<@U12345>") == ""

    def test_strips_mention_with_mixed_case_id(self):
        assert SlackConnector._strip_mention("<@U1A2B3C4D> test") == "test"


class TestHandleMention:
    """Test the mention event handler."""

    @pytest.mark.asyncio
    async def test_calls_execute_with_correct_args(self):
        """Execute is called with instance name, conversation_id, and stripped text."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "I am a response"

        config = make_config()
        connector = SlackConnector(config, mock_service)

        mock_say = AsyncMock()
        event = {
            "text": "<@UBOT123> What is Python?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_mention(event, mock_say)

        mock_service.execute.assert_called_once_with(
            "alpha",
            "C99999:1234567890.123456",
            "What is Python?",
        )

    @pytest.mark.asyncio
    async def test_posts_response_with_persona(self):
        """Response is posted in thread with the configured persona."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Python is a programming language."

        config = make_config()
        connector = SlackConnector(config, mock_service)

        mock_say = AsyncMock()
        event = {
            "text": "<@UBOT123> What is Python?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_mention(event, mock_say)

        mock_say.assert_called_once()
        call_kwargs = mock_say.call_args[1]
        assert call_kwargs["text"] == "Python is a programming language."
        assert call_kwargs["username"] == "Alpha"
        assert call_kwargs["icon_emoji"] == ":robot_face:"
        assert call_kwargs["thread_ts"] == "1234567890.123456"

    @pytest.mark.asyncio
    async def test_uses_thread_ts_for_replies(self):
        """When replying in a thread, use thread_ts as conversation key."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Response"

        config = make_config()
        connector = SlackConnector(config, mock_service)

        event = {
            "text": "<@UBOT123> follow up",
            "channel": "C99999",
            "ts": "1234567890.999999",        # This message's ts
            "thread_ts": "1234567890.123456",  # Parent thread ts
            "user": "U67890",
        }

        await connector._handle_mention(event, AsyncMock())

        call_args = mock_service.execute.call_args[0]
        assert call_args[1] == "C99999:1234567890.123456"  # Uses thread_ts

    @pytest.mark.asyncio
    async def test_posts_error_message_on_failure(self):
        """If execute raises, post a friendly error message."""
        mock_service = AsyncMock()
        mock_service.execute.side_effect = RuntimeError("LLM failed")

        config = make_config()
        connector = SlackConnector(config, mock_service)

        mock_say = AsyncMock()
        event = {
            "text": "<@UBOT123> do something",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_mention(event, mock_say)

        mock_say.assert_called_once()
        call_kwargs = mock_say.call_args[1]
        assert "error" in call_kwargs["text"].lower()
        assert call_kwargs["username"] == "Alpha"

    @pytest.mark.asyncio
    async def test_ignores_empty_text_after_stripping(self):
        """If the message is just a mention with no actual text, ignore it."""
        mock_service = AsyncMock()

        config = make_config()
        connector = SlackConnector(config, mock_service)

        event = {
            "text": "<@UBOT123>",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_mention(event, AsyncMock())

        mock_service.execute.assert_not_called()


class TestInstanceRouting:
    """Test /instance-name routing from mentions."""

    def test_parses_known_instance_prefix(self):
        name, text = SlackConnector._parse_instance_prefix(
            "/alpha review this code", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "review this code"

    def test_parses_different_instance(self):
        name, text = SlackConnector._parse_instance_prefix(
            "/beta what do you think?", ["alpha", "beta"], "alpha"
        )
        assert name == "beta"
        assert text == "what do you think?"

    def test_falls_back_to_default_for_no_prefix(self):
        name, text = SlackConnector._parse_instance_prefix(
            "just a question", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "just a question"

    def test_falls_back_for_unknown_prefix(self):
        """Unknown /name is treated as regular text, not a routing prefix."""
        name, text = SlackConnector._parse_instance_prefix(
            "/unknown hello", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "/unknown hello"

    def test_case_insensitive_matching(self):
        name, text = SlackConnector._parse_instance_prefix(
            "/Alpha review this", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "review this"

    @pytest.mark.asyncio
    async def test_mention_routes_to_specified_instance(self):
        """@bot /beta question routes to beta with beta's persona."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Beta's response"

        config = make_config()
        connector = SlackConnector(config, mock_service)

        mock_say = AsyncMock()
        event = {
            "text": "<@UBOT123> /beta what do you think?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_mention(event, mock_say)

        # Executed as beta
        mock_service.execute.assert_called_once_with(
            "beta", "C99999:1234567890.123456", "what do you think?"
        )

        # Posted with beta's persona
        call_kwargs = mock_say.call_args[1]
        assert call_kwargs["username"] == "Beta"
        assert call_kwargs["icon_emoji"] == ":gear:"

    @pytest.mark.asyncio
    async def test_mention_without_prefix_uses_default(self):
        """@bot question (no /name) routes to default instance."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Alpha's response"

        config = make_config()
        connector = SlackConnector(config, mock_service)

        mock_say = AsyncMock()
        event = {
            "text": "<@UBOT123> what time is it?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_mention(event, mock_say)

        mock_service.execute.assert_called_once_with(
            "alpha", "C99999:1234567890.123456", "what time is it?"
        )
        call_kwargs = mock_say.call_args[1]
        assert call_kwargs["username"] == "Alpha"


class TestChannelTopicParsing:
    """Test channel topic -> routing config."""

    def test_parses_instance_directive(self):
        config = SlackConnector._parse_channel_topic("[instance:alpha]", ["alpha", "beta"])
        assert config.instance == "alpha"
        assert config.mode is None
        assert config.default is None

    def test_parses_mode_directive(self):
        config = SlackConnector._parse_channel_topic("[mode:roundtable]", ["alpha", "beta"])
        assert config.mode == "roundtable"

    def test_parses_default_directive(self):
        config = SlackConnector._parse_channel_topic("[default:beta]", ["alpha", "beta"])
        assert config.default == "beta"

    def test_parses_mixed_topic_text(self):
        """Directives work alongside regular topic text."""
        config = SlackConnector._parse_channel_topic(
            "Coding help and architecture [instance:alpha]", ["alpha", "beta"]
        )
        assert config.instance == "alpha"

    def test_ignores_unknown_instance(self):
        config = SlackConnector._parse_channel_topic("[instance:unknown]", ["alpha", "beta"])
        assert config.instance is None

    def test_empty_topic_returns_empty_config(self):
        config = SlackConnector._parse_channel_topic("", ["alpha", "beta"])
        assert config.instance is None
        assert config.mode is None
        assert config.default is None

    def test_parses_multiple_directives(self):
        config = SlackConnector._parse_channel_topic(
            "[default:alpha] [mode:roundtable]", ["alpha", "beta"]
        )
        assert config.default == "alpha"
        assert config.mode == "roundtable"

    def test_case_insensitive(self):
        config = SlackConnector._parse_channel_topic("[Instance:Alpha]", ["alpha", "beta"])
        assert config.instance == "alpha"


class TestHandleMessage:
    """Test the channel message handler (non-mention messages)."""

    @pytest.mark.asyncio
    async def test_skips_bot_messages(self):
        """Messages from bots are ignored (prevents loops)."""
        mock_service = AsyncMock()
        config = make_config()
        connector = SlackConnector(config, mock_service)

        event = {
            "text": "I am a bot message",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "bot_id": "B12345",
        }

        await connector._handle_message(event, AsyncMock())
        mock_service.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_message_subtypes(self):
        """Messages with subtypes (edited, deleted, etc.) are ignored."""
        mock_service = AsyncMock()
        config = make_config()
        connector = SlackConnector(config, mock_service)

        event = {
            "text": "edited message",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "subtype": "message_changed",
        }

        await connector._handle_message(event, AsyncMock())
        mock_service.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_at_mentions(self):
        """Messages containing bot @mention are handled by _handle_mention, not here."""
        mock_service = AsyncMock()
        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"

        event = {
            "text": "<@UBOTID> hello",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_message(event, AsyncMock())
        mock_service.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_routes_in_single_instance_channel(self):
        """In a channel with [instance:alpha] topic, messages go to alpha."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Alpha's response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        # Pre-populate cache so we don't need real Slack API
        connector._channel_cache["C99999"] = ChannelConfig(instance="alpha")
        connector._cache_timestamps["C99999"] = time.time()

        mock_say = AsyncMock()
        event = {
            "text": "What is Python?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_message(event, mock_say)

        mock_service.execute.assert_called_once_with(
            "alpha", "C99999:1234567890.123456", "What is Python?"
        )
        call_kwargs = mock_say.call_args[1]
        assert call_kwargs["username"] == "Alpha"

    @pytest.mark.asyncio
    async def test_ignores_unconfigured_channel(self):
        """In a channel with no topic config, non-mention messages are ignored."""
        mock_service = AsyncMock()
        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        # Empty config = unconfigured
        connector._channel_cache["C99999"] = ChannelConfig()
        connector._cache_timestamps["C99999"] = time.time()

        event = {
            "text": "Hello?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_message(event, AsyncMock())
        mock_service.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_channel_with_prefix_override(self):
        """In [default:alpha] channel, /beta overrides to beta."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Beta's response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        connector._channel_cache["C99999"] = ChannelConfig(default="alpha")
        connector._cache_timestamps["C99999"] = time.time()

        mock_say = AsyncMock()
        event = {
            "text": "/beta what do you think?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_message(event, mock_say)

        mock_service.execute.assert_called_once_with(
            "beta", "C99999:1234567890.123456", "what do you think?"
        )
        call_kwargs = mock_say.call_args[1]
        assert call_kwargs["username"] == "Beta"
        assert call_kwargs["icon_emoji"] == ":gear:"
