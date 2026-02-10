"""Tests for SlackConnector."""

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
        """Execute is called with instance name, conversation_id, and enriched prompt."""
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

        mock_service.execute.assert_called_once()
        call_args = mock_service.execute.call_args[0]
        assert call_args[0] == "alpha"
        assert call_args[1] == "C99999:1234567890.123456"
        assert "What is Python?" in call_args[2]

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
            "ts": "1234567890.999999",  # This message's ts
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
        assert "not working" in call_kwargs["text"].lower()
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
    """Test natural instance addressing patterns."""

    def test_colon_pattern(self):
        """'alpha: review this' routes to alpha."""
        name, text, _ = SlackConnector._parse_instance_prefix(
            "alpha: review this code", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "review this code"

    def test_comma_pattern(self):
        """'beta, what do you think' routes to beta."""
        name, text, _ = SlackConnector._parse_instance_prefix(
            "beta, what do you think?", ["alpha", "beta"], "alpha"
        )
        assert name == "beta"
        assert text == "what do you think?"

    def test_at_pattern(self):
        """'@beta review this' routes to beta."""
        name, text, _ = SlackConnector._parse_instance_prefix(
            "@beta review this", ["alpha", "beta"], "alpha"
        )
        assert name == "beta"
        assert text == "review this"

    def test_hey_pattern(self):
        """'hey alpha, look at this' routes to alpha."""
        name, text, _ = SlackConnector._parse_instance_prefix(
            "hey alpha, look at this", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "look at this"

    def test_name_as_first_word(self):
        """'beta what do you think' routes to beta."""
        name, text, _ = SlackConnector._parse_instance_prefix(
            "beta what do you think?", ["alpha", "beta"], "alpha"
        )
        assert name == "beta"
        assert text == "what do you think?"

    def test_falls_back_to_default_for_no_name(self):
        name, text, _ = SlackConnector._parse_instance_prefix(
            "just a question", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "just a question"

    def test_no_false_positive_on_embedded_name(self):
        """'the alpha version is...' should NOT route to alpha."""
        name, text, _ = SlackConnector._parse_instance_prefix(
            "the alpha version is great", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "the alpha version is great"

    def test_case_insensitive_matching(self):
        name, text, _ = SlackConnector._parse_instance_prefix(
            "Alpha: review this", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "review this"

    @pytest.mark.asyncio
    async def test_mention_routes_to_specified_instance(self):
        """@bot beta: question routes to beta with beta's persona."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Beta's response"

        config = make_config()
        connector = SlackConnector(config, mock_service)

        mock_say = AsyncMock()
        event = {
            "text": "<@UBOT123> beta: what do you think?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_mention(event, mock_say)

        # Executed as beta with enriched prompt
        mock_service.execute.assert_called_once()
        call_args = mock_service.execute.call_args[0]
        assert call_args[0] == "beta"
        assert call_args[1] == "C99999:1234567890.123456"
        assert "what do you think?" in call_args[2]

        # Posted with beta's persona
        call_kwargs = mock_say.call_args[1]
        assert call_kwargs["username"] == "Beta"
        assert call_kwargs["icon_emoji"] == ":gear:"

    @pytest.mark.asyncio
    async def test_mention_without_prefix_uses_default(self):
        """@bot question (no name) routes to default instance."""
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

        mock_service.execute.assert_called_once()
        call_args = mock_service.execute.call_args[0]
        assert call_args[0] == "alpha"
        assert call_args[1] == "C99999:1234567890.123456"
        assert "what time is it?" in call_args[2]
        call_kwargs = mock_say.call_args[1]
        assert call_kwargs["username"] == "Alpha"


class TestChannelTopicParsing:
    """Test channel topic -> routing config."""

    def test_parses_instance_directive(self):
        config = SlackConnector._parse_channel_topic(
            "[instance:alpha]", ["alpha", "beta"]
        )
        assert config.instance == "alpha"
        assert config.mode is None
        assert config.default is None

    def test_parses_mode_directive(self):
        config = SlackConnector._parse_channel_topic(
            "[mode:roundtable]", ["alpha", "beta"]
        )
        assert config.mode == "roundtable"

    def test_parses_default_directive(self):
        config = SlackConnector._parse_channel_topic(
            "[default:beta]", ["alpha", "beta"]
        )
        assert config.default == "beta"

    def test_parses_mixed_topic_text(self):
        """Directives work alongside regular topic text."""
        config = SlackConnector._parse_channel_topic(
            "Coding help and architecture [instance:alpha]", ["alpha", "beta"]
        )
        assert config.instance == "alpha"

    def test_ignores_unknown_instance(self):
        config = SlackConnector._parse_channel_topic(
            "[instance:unknown]", ["alpha", "beta"]
        )
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
        config = SlackConnector._parse_channel_topic(
            "[Instance:Alpha]", ["alpha", "beta"]
        )
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

        mock_service.execute.assert_called_once()
        call_args = mock_service.execute.call_args[0]
        assert call_args[0] == "alpha"
        assert call_args[1] == "C99999:1234567890.123456"
        assert "What is Python?" in call_args[2]
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
            "text": "beta: what do you think?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_message(event, mock_say)

        mock_service.execute.assert_called_once()
        call_args = mock_service.execute.call_args[0]
        assert call_args[0] == "beta"
        assert call_args[1] == "C99999:1234567890.123456"
        assert "what do you think?" in call_args[2]
        call_kwargs = mock_say.call_args[1]
        assert call_kwargs["username"] == "Beta"
        assert call_kwargs["icon_emoji"] == ":gear:"


class TestBuildPrompt:
    """Test message context enrichment."""

    @pytest.mark.asyncio
    async def test_includes_user_and_channel(self):
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        result = connector._build_prompt(
            "What is Python?", "U12345", "C99999", "coding"
        )
        assert "<@U12345>" in result
        assert "#coding" in result
        assert "What is Python?" in result

    @pytest.mark.asyncio
    async def test_dm_context(self):
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        result = connector._build_prompt("Hello", "U12345", "D99999", "")
        assert "<@U12345>" in result
        assert "DM" in result
        assert "Hello" in result

    @pytest.mark.asyncio
    async def test_preserves_original_text(self):
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        result = connector._build_prompt(
            "Tell me about Rust", "U12345", "C99999", "coding"
        )
        assert "Tell me about Rust" in result


class TestChannelConfigName:
    """Test that ChannelConfig includes channel name."""

    def test_channel_config_has_name_field(self):
        config = ChannelConfig(name="general")
        assert config.name == "general"

    def test_channel_config_name_defaults_empty(self):
        config = ChannelConfig()
        assert config.name == ""


class TestContextEnrichmentInHandlers:
    """Test that handlers pass enriched prompts to execute()."""

    @pytest.mark.asyncio
    async def test_mention_sends_enriched_prompt(self):
        """_handle_mention sends context-enriched prompt to execute()."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "response"

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

        call_args = mock_service.execute.call_args[0]
        prompt = call_args[2]
        assert "<@U67890>" in prompt
        assert "What is Python?" in prompt

    @pytest.mark.asyncio
    async def test_message_sends_enriched_prompt(self):
        """_handle_message sends context-enriched prompt to execute()."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        connector._channel_cache["C99999"] = ChannelConfig(
            instance="alpha", name="coding"
        )
        connector._cache_timestamps["C99999"] = time.time()

        mock_say = AsyncMock()
        event = {
            "text": "What is Python?",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_message(event, mock_say)

        call_args = mock_service.execute.call_args[0]
        prompt = call_args[2]
        assert "<@U67890>" in prompt
        assert "#coding" in prompt
        assert "What is Python?" in prompt


class TestDMHandling:
    """Test DM message routing."""

    @pytest.mark.asyncio
    async def test_dm_routes_to_default_instance(self):
        """DM without instance name goes to default."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Hello from Alpha"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"

        mock_say = AsyncMock()
        event = {
            "text": "Hello",
            "channel": "D99999",
            "channel_type": "im",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_message(event, mock_say)

        mock_service.execute.assert_called_once()
        call_args = mock_service.execute.call_args[0]
        assert call_args[0] == "alpha"  # default instance
        assert call_args[1] == "dm:U67890"  # DM conversation ID

    @pytest.mark.asyncio
    async def test_dm_with_instance_prefix(self):
        """DM with 'beta: ...' routes to beta."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Hello from Beta"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"

        mock_say = AsyncMock()
        event = {
            "text": "beta: review this",
            "channel": "D99999",
            "channel_type": "im",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_message(event, mock_say)

        call_args = mock_service.execute.call_args[0]
        assert call_args[0] == "beta"

    @pytest.mark.asyncio
    async def test_dm_uses_dm_context_in_prompt(self):
        """DM prompt includes DM context, not channel name."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"

        mock_say = AsyncMock()
        event = {
            "text": "Hello",
            "channel": "D99999",
            "channel_type": "im",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_message(event, mock_say)

        call_args = mock_service.execute.call_args[0]
        prompt = call_args[2]
        assert "DM" in prompt
        assert "<@U67890>" in prompt
        assert "Hello" in prompt

    @pytest.mark.asyncio
    async def test_dm_posts_with_persona(self):
        """DM response uses instance persona."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Hi there!"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"

        mock_say = AsyncMock()
        event = {
            "text": "Hello",
            "channel": "D99999",
            "channel_type": "im",
            "ts": "1234567890.123456",
            "user": "U67890",
        }

        await connector._handle_message(event, mock_say)

        mock_say.assert_called_once()
        call_kwargs = mock_say.call_args[1]
        assert call_kwargs["username"] == "Alpha"
        assert call_kwargs["icon_emoji"] == ":robot_face:"


class TestReactionHandling:
    """Test emoji reaction commands."""

    @pytest.mark.asyncio
    async def test_regenerate_reaction(self):
        """🔄 reaction re-executes the original prompt."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Regenerated response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        # Simulate a previous message we can regenerate
        connector._message_prompts["1234567890.111111"] = (
            "alpha",
            "C99999:1234567890.000000",
            "What is Python?",
        )
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock()

        event = {
            "reaction": "repeat",
            "user": "U67890",
            "item": {
                "channel": "C99999",
                "ts": "1234567890.111111",
            },
        }

        await connector._handle_reaction(event, AsyncMock())

        mock_service.execute.assert_called_once_with(
            "alpha", "C99999:1234567890.000000", "What is Python?"
        )
        connector._app.client.chat_postMessage.assert_called_once()

    @pytest.mark.asyncio
    async def test_regenerate_arrows_counterclockwise(self):
        """arrows_counterclockwise also triggers regenerate."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Regenerated response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._message_prompts["1234567890.111111"] = (
            "alpha",
            "C99999:1234567890.000000",
            "What is Python?",
        )
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock()

        event = {
            "reaction": "arrows_counterclockwise",
            "user": "U67890",
            "item": {
                "channel": "C99999",
                "ts": "1234567890.111111",
            },
        }

        await connector._handle_reaction(event, AsyncMock())

        mock_service.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_reaction(self):
        """❌ reaction adds acknowledgment checkmark."""
        mock_service = AsyncMock()

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._message_prompts["1234567890.111111"] = (
            "alpha",
            "C99999:1234567890.000000",
            "What is Python?",
        )
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()

        event = {
            "reaction": "x",
            "user": "U67890",
            "item": {
                "channel": "C99999",
                "ts": "1234567890.111111",
            },
        }

        await connector._handle_reaction(event, AsyncMock())

        mock_service.execute.assert_not_called()
        connector._app.client.reactions_add.assert_called_once()
        call_kwargs = connector._app.client.reactions_add.call_kwargs
        # Just verify it was called (kwargs checked via assert_called_once)

    @pytest.mark.asyncio
    async def test_ignores_reaction_on_non_bot_message(self):
        """Reactions on messages we didn't send are ignored."""
        mock_service = AsyncMock()
        config = make_config()
        connector = SlackConnector(config, mock_service)
        # No message in _message_prompts for this ts

        event = {
            "reaction": "repeat",
            "user": "U67890",
            "item": {"channel": "C99999", "ts": "9999999999.999999"},
        }

        await connector._handle_reaction(event, AsyncMock())
        mock_service.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_unrecognized_reaction(self):
        """Random reactions on bot messages are ignored."""
        mock_service = AsyncMock()
        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._message_prompts["1234567890.111111"] = (
            "alpha",
            "C99999:1234567890.000000",
            "What is Python?",
        )

        event = {
            "reaction": "thumbsup",
            "user": "U67890",
            "item": {"channel": "C99999", "ts": "1234567890.111111"},
        }

        await connector._handle_reaction(event, AsyncMock())
        mock_service.execute.assert_not_called()


class TestFileUpload:
    """Test file download from Slack to workspace."""

    @pytest.mark.asyncio
    async def test_file_share_message_downloads_file(self, tmp_path):
        """File upload events trigger download to working directory."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "I see your file"

        config = make_config()
        config.instances["alpha"].working_dir = str(tmp_path)

        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        connector._channel_cache["C99999"] = ChannelConfig(
            instance="alpha", name="test"
        )
        connector._cache_timestamps["C99999"] = time.time()

        event = {
            "text": "check this out",
            "subtype": "file_share",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
            "files": [
                {
                    "id": "F123",
                    "name": "report.pdf",
                    "size": 1024,
                    "url_private": "https://files.slack.com/files-pri/T123/report.pdf",
                    "mimetype": "application/pdf",
                }
            ],
        }

        with patch.object(
            connector, "_download_slack_file", new_callable=AsyncMock
        ) as mock_dl:
            mock_dl.return_value = tmp_path / "report.pdf"
            await connector._handle_message(event, AsyncMock())

        mock_service.execute.assert_called_once()
        prompt = mock_service.execute.call_args[0][2]
        assert "report.pdf" in prompt
        assert "uploaded" in prompt.lower()

    @pytest.mark.asyncio
    async def test_file_only_message_not_skipped(self, tmp_path):
        """Messages with files but no text are processed."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Got your file"

        config = make_config()
        config.instances["alpha"].working_dir = str(tmp_path)

        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        connector._channel_cache["C99999"] = ChannelConfig(
            instance="alpha", name="test"
        )
        connector._cache_timestamps["C99999"] = time.time()

        event = {
            "text": "",
            "subtype": "file_share",
            "channel": "C99999",
            "ts": "1234567890.123456",
            "user": "U67890",
            "files": [
                {
                    "id": "F123",
                    "name": "data.csv",
                    "size": 512,
                    "url_private": "https://files.slack.com/files-pri/T123/data.csv",
                }
            ],
        }

        with patch.object(
            connector, "_download_slack_file", new_callable=AsyncMock
        ) as mock_dl:
            mock_dl.return_value = tmp_path / "data.csv"
            await connector._handle_message(event, AsyncMock())

        mock_service.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_download_skips_oversized_files(self, tmp_path):
        """Files over 50MB are skipped."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())

        result = await connector._download_slack_file(
            {
                "name": "huge.zip",
                "size": 100 * 1024 * 1024,
                "url_private": "https://example.com",
            },
            tmp_path,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_download_skips_missing_url(self, tmp_path):
        """Files without url_private are skipped."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())

        result = await connector._download_slack_file(
            {"name": "nourl.txt", "size": 100},
            tmp_path,
        )
        assert result is None


class TestFileOutbox:
    """Test .outbox/ file sharing back to Slack."""

    @pytest.mark.asyncio
    async def test_process_outbox_uploads_and_deletes(self, tmp_path):
        """Files in .outbox/ are uploaded to Slack and removed."""
        outbox = tmp_path / ".outbox"
        outbox.mkdir()
        test_file = outbox / "result.csv"
        test_file.write_text("a,b,c\n1,2,3")

        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.files_upload_v2 = AsyncMock()

        await connector._process_outbox(
            tmp_path,
            "C99999",
            "1234567890.123456",
            config.instances["alpha"],
        )

        connector._app.client.files_upload_v2.assert_called_once()
        assert not test_file.exists()

    @pytest.mark.asyncio
    async def test_process_outbox_noop_when_empty(self, tmp_path):
        """No-op when .outbox/ is empty or doesn't exist."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())

        await connector._process_outbox(
            tmp_path,
            "C99999",
            "1234567890.123456",
            config.instances["alpha"],
        )
        # Should not crash

    @pytest.mark.asyncio
    async def test_process_outbox_skips_dotfiles(self, tmp_path):
        """Dotfiles in .outbox/ are ignored."""
        outbox = tmp_path / ".outbox"
        outbox.mkdir()
        (outbox / ".gitkeep").write_text("")
        (outbox / "real_file.txt").write_text("hello")

        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.files_upload_v2 = AsyncMock()

        await connector._process_outbox(
            tmp_path,
            "C99999",
            "1234567890.123456",
            config.instances["alpha"],
        )

        connector._app.client.files_upload_v2.assert_called_once()
        call_kwargs = connector._app.client.files_upload_v2.call_args[1]
        assert "real_file.txt" in call_kwargs["file"]


class TestBuildPromptWithFiles:
    """Test _build_prompt with file descriptions."""

    @pytest.mark.asyncio
    async def test_includes_file_descriptions(self):
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        result = connector._build_prompt(
            "check this",
            "U123",
            "C456",
            "coding",
            file_descriptions="[User uploaded files:\n  report.pdf (1024 bytes) → ./report.pdf]",
        )
        assert "report.pdf" in result
        assert "uploaded" in result.lower()
        assert "check this" in result

    @pytest.mark.asyncio
    async def test_includes_outbox_instruction(self):
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        result = connector._build_prompt("hello", "U123", "C456", "coding")
        assert ".outbox/" in result

    @pytest.mark.asyncio
    async def test_handles_empty_text_with_files(self):
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        result = connector._build_prompt(
            "",
            "U123",
            "C456",
            "coding",
            file_descriptions="[User uploaded: data.csv]",
        )
        assert "data.csv" in result
        assert ".outbox/" in result


class TestFriendlyToolNames:
    """Test tool name to human-friendly description mapping."""

    def test_known_tool_names(self):
        from hive_slack.slack import _friendly_tool_name

        assert "Reading" in _friendly_tool_name("read_file")
        assert "Running" in _friendly_tool_name("bash")
        assert "Searching" in _friendly_tool_name("web_search")

    def test_unknown_tool_returns_working(self):
        from hive_slack.slack import _friendly_tool_name

        result = _friendly_tool_name("unknown_tool")
        assert "Working" in result
        assert "unknown_tool" in result

    def test_all_common_tools_have_friendly_names(self):
        from hive_slack.slack import _friendly_tool_name

        common_tools = [
            "read_file",
            "write_file",
            "edit_file",
            "bash",
            "glob",
            "grep",
            "web_search",
            "web_fetch",
            "delegate",
            "todo",
            "LSP",
            "python_check",
            "load_skill",
            "recipes",
        ]
        for tool in common_tools:
            result = _friendly_tool_name(tool)
            # Should NOT contain "Working (" — that's the fallback for unknowns
            assert not result.startswith("Working ("), f"{tool} has no friendly name"


class TestProgressIndicators:
    """Test status messages and execution tracking."""

    @pytest.mark.asyncio
    async def test_execute_with_progress_adds_hourglass_reaction(self):
        """Hourglass reaction is added to user's message at start."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "status123"}
        )
        connector._app.client.chat_delete = AsyncMock()

        instance = config.get_instance("alpha")
        mock_say = AsyncMock()

        await connector._execute_with_progress(
            "alpha",
            instance,
            "C99999:1234567890.000000",
            "hello",
            "C99999",
            "1234567890.000000",
            "1234567890.000000",
            mock_say,
        )

        # Check hourglass reaction was added
        connector._app.client.reactions_add.assert_any_call(
            channel="C99999",
            timestamp="1234567890.000000",
            name="hourglass_flowing_sand",
        )

    @pytest.mark.asyncio
    async def test_execute_with_progress_posts_status_message(self):
        """A status message is posted before execution."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "status123"}
        )
        connector._app.client.chat_delete = AsyncMock()

        instance = config.get_instance("alpha")
        mock_say = AsyncMock()

        await connector._execute_with_progress(
            "alpha",
            instance,
            "C99999:1234567890.000000",
            "hello",
            "C99999",
            "1234567890.000000",
            "1234567890.000000",
            mock_say,
        )

        # Status message posted
        connector._app.client.chat_postMessage.assert_called_once()
        call_kwargs = connector._app.client.chat_postMessage.call_args[1]
        assert "Working" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_execute_with_progress_deletes_status_on_success(self):
        """Status message is deleted after successful execution."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "status123"}
        )
        connector._app.client.chat_delete = AsyncMock()

        instance = config.get_instance("alpha")
        mock_say = AsyncMock()

        await connector._execute_with_progress(
            "alpha",
            instance,
            "C99999:1234567890.000000",
            "hello",
            "C99999",
            "1234567890.000000",
            "1234567890.000000",
            mock_say,
        )

        # Status message deleted
        connector._app.client.chat_delete.assert_called_once_with(
            channel="C99999",
            ts="status123",
        )

    @pytest.mark.asyncio
    async def test_execute_with_progress_removes_hourglass_on_done(self):
        """Hourglass reaction is removed after execution completes."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "status123"}
        )
        connector._app.client.chat_delete = AsyncMock()

        instance = config.get_instance("alpha")
        mock_say = AsyncMock()

        await connector._execute_with_progress(
            "alpha",
            instance,
            "C99999:1234567890.000000",
            "hello",
            "C99999",
            "1234567890.000000",
            "1234567890.000000",
            mock_say,
        )

        # Hourglass removed
        connector._app.client.reactions_remove.assert_called_once_with(
            channel="C99999",
            timestamp="1234567890.000000",
            name="hourglass_flowing_sand",
        )

    @pytest.mark.asyncio
    async def test_execute_with_progress_posts_response_with_persona(self):
        """Final response uses instance persona."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "the answer"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "status123"}
        )
        connector._app.client.chat_delete = AsyncMock()

        instance = config.get_instance("alpha")
        mock_say = AsyncMock()

        await connector._execute_with_progress(
            "alpha",
            instance,
            "C99999:1234567890.000000",
            "hello",
            "C99999",
            "1234567890.000000",
            "1234567890.000000",
            mock_say,
        )

        mock_say.assert_called_once()
        call_kwargs = mock_say.call_args[1]
        assert call_kwargs["username"] == "Alpha"
        assert call_kwargs["icon_emoji"] == ":robot_face:"
        assert call_kwargs["text"] == "the answer"

    @pytest.mark.asyncio
    async def test_execute_with_progress_clears_active_execution(self):
        """Active execution is cleared after completion."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "status123"}
        )
        connector._app.client.chat_delete = AsyncMock()

        instance = config.get_instance("alpha")
        mock_say = AsyncMock()

        conv_id = "C99999:1234567890.000000"
        await connector._execute_with_progress(
            "alpha",
            instance,
            conv_id,
            "hello",
            "C99999",
            "1234567890.000000",
            "1234567890.000000",
            mock_say,
        )

        # Active execution should be cleared
        assert conv_id not in connector._active_executions

    @pytest.mark.asyncio
    async def test_execute_with_progress_handles_error(self):
        """On execution error, status is deleted and error message posted."""
        mock_service = AsyncMock()
        mock_service.execute.side_effect = RuntimeError("boom")

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "status123"}
        )
        connector._app.client.chat_delete = AsyncMock()

        instance = config.get_instance("alpha")
        mock_say = AsyncMock()

        conv_id = "C99999:1234567890.000000"
        await connector._execute_with_progress(
            "alpha",
            instance,
            conv_id,
            "hello",
            "C99999",
            "1234567890.000000",
            "1234567890.000000",
            mock_say,
        )

        # Status message deleted on error
        connector._app.client.chat_delete.assert_called_once()
        # Error message posted with persona
        mock_say.assert_called_once()
        call_kwargs = mock_say.call_args[1]
        assert "not working" in call_kwargs["text"].lower()
        assert call_kwargs["username"] == "Alpha"
        # Active execution cleared
        assert conv_id not in connector._active_executions


class TestMessageQueuing:
    """Test message queuing when conversation is busy."""

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_active_execution_injects_or_queues_messages(self):
        """Messages to a busy conversation are injected or queued, not executed."""
        mock_service = AsyncMock()
        # inject_message returns True (injection succeeded)
        mock_service.inject_message = MagicMock(return_value=True)
        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        connector._channel_cache["C99999"] = ChannelConfig(
            instance="alpha", name="test"
        )
        connector._cache_timestamps["C99999"] = time.time()

        # Simulate an active execution
        conv_id = "C99999:1234567890.000000"
        connector._active_executions[conv_id] = {
            "status_ts": "status123",
            "channel": "C99999",
            "thread_ts": "1234567890.000000",
            "instance_name": "alpha",
            "user_ts": "1234567890.000000",
        }
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()

        event = {
            "text": "Also check the tests",
            "channel": "C99999",
            "ts": "1234567890.111111",
            "thread_ts": "1234567890.000000",
            "user": "U67890",
        }

        await connector._handle_message(event, AsyncMock())

        # Should NOT have called execute
        mock_service.execute.assert_not_called()
        # Should have tried injection
        mock_service.inject_message.assert_called_once()
        # Should have reacted with 📨
        connector._app.client.reactions_add.assert_called_once()
        call_kwargs = connector._app.client.reactions_add.call_args[1]
        assert call_kwargs["name"] == "incoming_envelope"

    @pytest.mark.asyncio
    async def test_active_execution_falls_back_to_queue(self):
        """If injection fails, message is queued locally."""
        mock_service = AsyncMock()
        # inject_message returns False (injection not supported)
        mock_service.inject_message = MagicMock(return_value=False)
        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        connector._channel_cache["C99999"] = ChannelConfig(
            instance="alpha", name="test"
        )
        connector._cache_timestamps["C99999"] = time.time()

        conv_id = "C99999:1234567890.000000"
        connector._active_executions[conv_id] = {
            "status_ts": "status123",
            "channel": "C99999",
            "thread_ts": "1234567890.000000",
            "instance_name": "alpha",
            "user_ts": "1234567890.000000",
        }
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()

        event = {
            "text": "Also check the tests",
            "channel": "C99999",
            "ts": "1234567890.111111",
            "thread_ts": "1234567890.000000",
            "user": "U67890",
        }

        await connector._handle_message(event, AsyncMock())

        # Should have queued (injection failed)
        assert len(connector._message_queues.get(conv_id, [])) == 1

    @pytest.mark.asyncio
    async def test_mention_active_execution_injects_or_queues(self):
        """Mentions to a busy conversation are injected or queued, not executed."""
        mock_service = AsyncMock()
        mock_service.inject_message = MagicMock(return_value=True)
        config = make_config()
        connector = SlackConnector(config, mock_service)

        # Simulate an active execution
        conv_id = "C99999:1234567890.000000"
        connector._active_executions[conv_id] = {
            "status_ts": "status123",
            "channel": "C99999",
            "thread_ts": "1234567890.000000",
            "instance_name": "alpha",
            "user_ts": "1234567890.000000",
        }
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()

        event = {
            "text": "<@UBOT123> Also check the tests",
            "channel": "C99999",
            "ts": "1234567890.111111",
            "thread_ts": "1234567890.000000",
            "user": "U67890",
        }

        await connector._handle_mention(event, AsyncMock())

        # Should NOT have called execute
        mock_service.execute.assert_not_called()
        # Should have tried injection
        mock_service.inject_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_queued_messages_batched_after_execution(self):
        """Queued messages are batched into a follow-up execution."""
        mock_service = AsyncMock()
        # First call returns response, second call (batch) returns response
        mock_service.execute.return_value = "response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "status123"}
        )
        connector._app.client.chat_delete = AsyncMock()

        instance = config.get_instance("alpha")
        mock_say = AsyncMock()
        conv_id = "C99999:1234567890.000000"

        # Pre-queue a message
        connector._message_queues[conv_id] = ["also check the tests"]

        await connector._execute_with_progress(
            "alpha",
            instance,
            conv_id,
            "hello",
            "C99999",
            "1234567890.000000",
            "1234567890.000000",
            mock_say,
        )

        # execute should have been called twice: once for original, once for batch
        assert mock_service.execute.call_count == 2
        # The second call should contain the queued message
        batch_prompt = mock_service.execute.call_args_list[1][0][2]
        assert "also check the tests" in batch_prompt
        # Queue should be empty after processing
        assert conv_id not in connector._message_queues

    @pytest.mark.asyncio
    async def test_multiple_queued_messages_combined(self):
        """Multiple queued messages are combined into one batch prompt."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "response"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(
            return_value={"ts": "status123"}
        )
        connector._app.client.chat_delete = AsyncMock()

        instance = config.get_instance("alpha")
        mock_say = AsyncMock()
        conv_id = "C99999:1234567890.000000"

        # Pre-queue multiple messages
        connector._message_queues[conv_id] = ["msg one", "msg two", "msg three"]

        await connector._execute_with_progress(
            "alpha",
            instance,
            conv_id,
            "hello",
            "C99999",
            "1234567890.000000",
            "1234567890.000000",
            mock_say,
        )

        # The batch prompt should contain all three
        batch_prompt = mock_service.execute.call_args_list[1][0][2]
        assert "msg one" in batch_prompt
        assert "msg two" in batch_prompt
        assert "msg three" in batch_prompt


# ---------------------------------------------------------------------------
# Milestone 4 — Thread Ownership, Emoji Summoning, Roundtable Mode
# ---------------------------------------------------------------------------


class TestParseInstancePrefixThreeTuple:
    """Test 3-tuple return from _parse_instance_prefix."""

    def test_explicit_name_colon(self):
        name, text, explicit = SlackConnector._parse_instance_prefix(
            "alpha: review this", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "review this"
        assert explicit is True

    def test_explicit_at_name(self):
        name, text, explicit = SlackConnector._parse_instance_prefix(
            "@beta look at this", ["alpha", "beta"], "alpha"
        )
        assert name == "beta"
        assert explicit is True

    def test_no_match_returns_default(self):
        name, text, explicit = SlackConnector._parse_instance_prefix(
            "just a question", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "just a question"
        assert explicit is False


class TestThreadOwnership:
    """Test thread ownership tracking and routing."""

    @pytest.mark.asyncio
    async def test_set_and_get_owner(self):
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector._set_thread_owner("C1:t1", "alpha")
        assert connector._get_thread_owner("C1:t1") == "alpha"

    @pytest.mark.asyncio
    async def test_no_owner_returns_none(self):
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        assert connector._get_thread_owner("C1:t1") is None

    @pytest.mark.asyncio
    async def test_ownership_transfer(self):
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector._set_thread_owner("C1:t1", "alpha")
        connector._set_thread_owner("C1:t1", "beta")
        assert connector._get_thread_owner("C1:t1") == "beta"

    @pytest.mark.asyncio
    async def test_bounded_eviction(self):
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        # Fill to limit
        for i in range(10_001):
            connector._set_thread_owner(f"C1:t{i}", "alpha")
        # First entry should be evicted
        assert connector._get_thread_owner("C1:t0") is None
        # Last entry should exist
        assert connector._get_thread_owner("C1:t10000") == "alpha"


class TestEmojiSummoning:
    """Test emoji reaction summoning."""

    @pytest.mark.asyncio
    async def test_instance_name_emoji_triggers_summon(self):
        """Reacting with an instance-name emoji triggers summoning."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "Here's my analysis..."
        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.conversations_history = AsyncMock(return_value={
            "messages": [{"text": "Check this code", "ts": "msg_ts_123"}]
        })
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(return_value={"ts": "status_ts"})
        connector._app.client.chat_delete = AsyncMock()
        connector._app.client.conversations_info = AsyncMock(return_value={
            "channel": {"name": "general", "topic": {"value": ""}}
        })
        connector._cache_timestamps["C99999"] = time.time()
        connector._channel_cache["C99999"] = ChannelConfig(name="general")

        event = {
            "reaction": "alpha",
            "item": {"channel": "C99999", "ts": "msg_ts_123"},
            "user": "U_HUMAN",
        }

        mock_say = AsyncMock(return_value={"ts": "response_ts"})
        await connector._handle_reaction(event, mock_say)

        # Should have fetched the message
        connector._app.client.conversations_history.assert_called_once()
        # Should have called execute
        mock_service.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_instance_emoji_ignored(self):
        """Non-instance emoji reactions are not treated as summons."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector._bot_user_id = "UBOTID"

        event = {
            "reaction": "thumbsup",
            "item": {"channel": "C99999", "ts": "msg_ts"},
            "user": "U_HUMAN",
        }

        # Should not crash, should just return
        await connector._handle_reaction(event, AsyncMock())

    @pytest.mark.asyncio
    async def test_bot_self_reaction_ignored(self):
        """Bot's own reactions don't trigger summons."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector._bot_user_id = "UBOTID"

        event = {
            "reaction": "alpha",
            "item": {"channel": "C99999", "ts": "msg_ts"},
            "user": "UBOTID",  # Bot itself
        }

        await connector._handle_reaction(event, AsyncMock())
        # No execute call — bot ignored itself


class TestRoundtable:
    """Test roundtable mode."""

    @pytest.mark.asyncio
    async def test_build_roundtable_prompt(self):
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        prompt = connector._build_roundtable_prompt("What is caching?", "alpha")
        assert "ROUNDTABLE" in prompt
        assert "beta" in prompt  # other instance mentioned
        assert "[PASS]" in prompt
        assert "What is caching?" in prompt

    @pytest.mark.asyncio
    async def test_pass_response_filtered(self):
        """[PASS] responses from instances are not posted."""
        mock_service = AsyncMock()
        # alpha passes, beta responds
        async def mock_execute(instance, conv, prompt, **kwargs):
            if instance == "alpha":
                return "[PASS]"
            return "Here's my perspective on caching..."
        mock_service.execute = mock_execute

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(return_value={"ts": "status_ts"})
        connector._app.client.chat_delete = AsyncMock()

        mock_say = AsyncMock(return_value={"ts": "resp_ts"})

        await connector._execute_roundtable(
            "C1:t1", "What is caching?", "C1", "t1", "user_ts", mock_say,
        )

        # say should be called only once (beta's response, not alpha's [PASS])
        assert mock_say.call_count == 1
        call_kwargs = mock_say.call_args[1]
        assert "perspective" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_all_pass_no_response(self):
        """When all instances pass, no response is posted."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "[PASS]"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(return_value={"ts": "status_ts"})
        connector._app.client.chat_delete = AsyncMock()

        mock_say = AsyncMock()

        await connector._execute_roundtable(
            "C1:t1", "Thanks!", "C1", "t1", "user_ts", mock_say,
        )

        # say should NOT be called (all passed)
        mock_say.assert_not_called()

    @pytest.mark.asyncio
    async def test_roundtable_sets_thread_owner(self):
        """Roundtable execution marks thread as _ROUNDTABLE."""
        mock_service = AsyncMock()
        mock_service.execute.return_value = "[PASS]"

        config = make_config()
        connector = SlackConnector(config, mock_service)
        connector._bot_user_id = "UBOTID"
        connector._app = AsyncMock()
        connector._app.client = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._app.client.chat_postMessage = AsyncMock(return_value={"ts": "status_ts"})
        connector._app.client.chat_delete = AsyncMock()

        await connector._execute_roundtable(
            "C1:t1", "Hello", "C1", "t1", "user_ts", AsyncMock(),
        )

        assert connector._get_thread_owner("C1:t1") == "_ROUNDTABLE"


class TestFormatDuration:
    """Test duration formatting."""

    def test_under_10_seconds_empty(self):
        from hive_slack.slack import _format_duration
        assert _format_duration(5.0) == ""

    def test_seconds(self):
        from hive_slack.slack import _format_duration
        assert _format_duration(30.0) == "30s"

    def test_minutes_and_seconds(self):
        from hive_slack.slack import _format_duration
        assert _format_duration(90.0) == "1m 30s"

    def test_exact_minutes(self):
        from hive_slack.slack import _format_duration
        assert _format_duration(120.0) == "2m"

    def test_zero(self):
        from hive_slack.slack import _format_duration
        assert _format_duration(0.0) == ""


class TestRenderTodoStatus:
    """Test plan-mode status message rendering."""

    def test_basic_rendering(self):
        from hive_slack.slack import _render_todo_status
        todos = [
            {"content": "Read files", "status": "completed", "activeForm": "Reading files"},
            {"content": "Analyze code", "status": "in_progress", "activeForm": "Analyzing code"},
            {"content": "Write report", "status": "pending", "activeForm": "Writing report"},
        ]
        result = _render_todo_status(todos, "read_file", "Alpha", "45s", 0)
        assert "✅" in result
        assert "▸" in result
        assert "○" in result
        assert "Alpha" in result
        assert "45s" in result
        assert "1 of 3" in result

    def test_truncates_many_completed(self):
        from hive_slack.slack import _render_todo_status
        todos = [
            {"content": f"Task {i}", "status": "completed", "activeForm": f"Task {i}"}
            for i in range(5)
        ] + [
            {"content": "Current", "status": "in_progress", "activeForm": "Working"},
        ]
        result = _render_todo_status(todos, "bash", "Alpha", "1m", 0)
        assert "5 completed" in result
        assert "▸" in result

    def test_truncates_many_pending(self):
        from hive_slack.slack import _render_todo_status
        todos = [
            {"content": "Done", "status": "completed", "activeForm": "Done"},
            {"content": "Current", "status": "in_progress", "activeForm": "Working"},
        ] + [
            {"content": f"Pending {i}", "status": "pending", "activeForm": f"Pending {i}"}
            for i in range(5)
        ]
        result = _render_todo_status(todos, "bash", "Alpha", "", 0)
        assert "+3 more" in result

    def test_shows_queued_messages(self):
        from hive_slack.slack import _render_todo_status
        todos = [
            {"content": "Task", "status": "in_progress", "activeForm": "Working"},
        ]
        result = _render_todo_status(todos, "bash", "Alpha", "", 2)
        assert "2 messages queued" in result

    def test_no_tool_shows_thinking(self):
        from hive_slack.slack import _render_todo_status
        todos = [
            {"content": "Task", "status": "in_progress", "activeForm": "Working"},
        ]
        result = _render_todo_status(todos, "", "Alpha", "", 0)
        assert "Thinking" in result

    def test_delegate_tool_text(self):
        from hive_slack.slack import _render_todo_status
        todos = [
            {"content": "Task", "status": "in_progress", "activeForm": "Working"},
        ]
        result = _render_todo_status(todos, "delegate", "Alpha", "", 0)
        assert "Delegating" in result

    def test_uses_active_form_for_in_progress(self):
        from hive_slack.slack import _render_todo_status
        todos = [
            {"content": "Run tests", "status": "in_progress", "activeForm": "Running tests"},
        ]
        result = _render_todo_status(todos, "", "Alpha", "", 0)
        assert "Running tests" in result

    def test_header_without_duration(self):
        from hive_slack.slack import _render_todo_status
        todos = [
            {"content": "Task", "status": "pending", "activeForm": "Working"},
        ]
        result = _render_todo_status(todos, "", "Alpha", "", 0)
        assert result.startswith("⚙️ Alpha\n")  # No duration appended


class TestReconnect:
    """Test the reconnect method for refreshing Socket Mode connections."""

    @pytest.mark.asyncio
    async def test_reconnect_closes_old_and_opens_new(self):
        """Reconnect closes the old handler and creates a fresh one."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector._handler = AsyncMock()
        connector._app = MagicMock()

        with patch("hive_slack.slack.AsyncSocketModeHandler") as MockHandler:
            new_handler = AsyncMock()
            MockHandler.return_value = new_handler

            await connector.reconnect()

            # New handler was created with correct args
            MockHandler.assert_called_once_with(connector._app, config.slack.app_token)
            # New handler was connected
            new_handler.connect_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconnect_survives_close_error(self):
        """If closing the old handler fails, reconnect still creates a new one."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        old_handler = AsyncMock()
        old_handler.close_async.side_effect = RuntimeError("socket gone")
        connector._handler = old_handler
        connector._app = MagicMock()

        with patch("hive_slack.slack.AsyncSocketModeHandler") as MockHandler:
            new_handler = AsyncMock()
            MockHandler.return_value = new_handler

            await connector.reconnect()

            # Should still succeed despite close error
            new_handler.connect_async.assert_called_once()


class TestConnectionWatchdog:
    """Test the connection watchdog for suspend/resume detection."""

    @pytest.mark.asyncio
    async def test_detects_time_jump_and_reconnects(self):
        """A wall-clock jump triggers reconnect (simulates OS suspend/resume)."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector.reconnect = AsyncMock()

        # time.time() is called once for init (last_wall) and once per loop
        # iteration (now_wall). A 300s jump between init and first loop tick
        # with near-zero monotonic elapsed triggers the reconnect.
        # Call sequence: init=1000, after first sleep=1300 (jumped!)
        wall_times = [1000.0, 1300.0]
        time_call = 0

        def fake_time():
            nonlocal time_call
            idx = min(time_call, len(wall_times) - 1)
            time_call += 1
            return wall_times[idx]

        sleep_count = 0

        async def fake_sleep(_interval):
            nonlocal sleep_count
            sleep_count += 1
            # Let the first sleep pass so the jump detection runs,
            # then cancel on the second to exit the loop.
            if sleep_count >= 2:
                raise asyncio.CancelledError

        with (
            patch("asyncio.sleep", side_effect=fake_sleep),
            patch("time.time", side_effect=fake_time),
        ):
            with pytest.raises(asyncio.CancelledError):
                await connector.run_watchdog(interval=15.0)

        connector.reconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_reconnect_on_normal_tick(self):
        """Normal ticks without time jumps do not trigger reconnect."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector.reconnect = AsyncMock()

        iteration = 0

        async def fake_sleep(_interval):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await connector.run_watchdog(interval=15.0)

        connector.reconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_health_check_triggers_after_8_intervals(self):
        """auth.test health check fires every 8 intervals (~2 minutes)."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector.reconnect = AsyncMock()
        connector._app = AsyncMock()
        connector._app.client.auth_test = AsyncMock()

        iteration = 0

        async def fake_sleep(_interval):
            nonlocal iteration
            iteration += 1
            if iteration >= 9:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await connector.run_watchdog(interval=15.0)

        # auth.test should have been called once (at iteration 8)
        connector._app.client.auth_test.assert_called_once()
        # But no reconnect (health check passed)
        connector.reconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_health_check_failure_triggers_reconnect(self):
        """Failed auth.test triggers reconnect."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector.reconnect = AsyncMock()
        connector._app = AsyncMock()
        connector._app.client.auth_test = AsyncMock(
            side_effect=Exception("connection lost")
        )

        iteration = 0

        async def fake_sleep(_interval):
            nonlocal iteration
            iteration += 1
            if iteration >= 9:
                raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await connector.run_watchdog(interval=15.0)

        # Health check failed, so reconnect should have been called
        connector.reconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconnect_failure_does_not_crash_watchdog(self):
        """If reconnect raises, the watchdog continues running."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())
        connector.reconnect = AsyncMock(side_effect=RuntimeError("reconnect failed"))
        connector._app = AsyncMock()
        connector._app.client.auth_test = AsyncMock(
            side_effect=Exception("connection lost")
        )

        iteration = 0

        async def fake_sleep(_interval):
            nonlocal iteration
            iteration += 1
            if iteration >= 17:
                raise asyncio.CancelledError  # Let it run past 2 health checks

        with patch("asyncio.sleep", side_effect=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await connector.run_watchdog(interval=15.0)

        # Should have attempted reconnect twice (at iteration 8 and 16)
        assert connector.reconnect.call_count == 2
