"""Tests for SlackConnector."""

import time

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
        name, text = SlackConnector._parse_instance_prefix(
            "alpha: review this code", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "review this code"

    def test_comma_pattern(self):
        """'beta, what do you think' routes to beta."""
        name, text = SlackConnector._parse_instance_prefix(
            "beta, what do you think?", ["alpha", "beta"], "alpha"
        )
        assert name == "beta"
        assert text == "what do you think?"

    def test_at_pattern(self):
        """'@beta review this' routes to beta."""
        name, text = SlackConnector._parse_instance_prefix(
            "@beta review this", ["alpha", "beta"], "alpha"
        )
        assert name == "beta"
        assert text == "review this"

    def test_hey_pattern(self):
        """'hey alpha, look at this' routes to alpha."""
        name, text = SlackConnector._parse_instance_prefix(
            "hey alpha, look at this", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "look at this"

    def test_name_as_first_word(self):
        """'beta what do you think' routes to beta."""
        name, text = SlackConnector._parse_instance_prefix(
            "beta what do you think?", ["alpha", "beta"], "alpha"
        )
        assert name == "beta"
        assert text == "what do you think?"

    def test_falls_back_to_default_for_no_name(self):
        name, text = SlackConnector._parse_instance_prefix(
            "just a question", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "just a question"

    def test_no_false_positive_on_embedded_name(self):
        """'the alpha version is...' should NOT route to alpha."""
        name, text = SlackConnector._parse_instance_prefix(
            "the alpha version is great", ["alpha", "beta"], "alpha"
        )
        assert name == "alpha"
        assert text == "the alpha version is great"

    def test_case_insensitive_matching(self):
        name, text = SlackConnector._parse_instance_prefix(
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
        result = connector._build_prompt("What is Python?", "U12345", "C99999", "coding")
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
        result = connector._build_prompt("Tell me about Rust", "U12345", "C99999", "coding")
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
        connector._channel_cache["C99999"] = ChannelConfig(instance="alpha", name="coding")
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
            "alpha", "C99999:1234567890.000000", "What is Python?"
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
            "alpha", "C99999:1234567890.000000", "What is Python?"
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
            "alpha", "C99999:1234567890.000000", "What is Python?"
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
            "alpha", "C99999:1234567890.000000", "What is Python?"
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
        connector._channel_cache["C99999"] = ChannelConfig(instance="alpha", name="test")
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

        with patch.object(connector, "_download_slack_file", new_callable=AsyncMock) as mock_dl:
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
        connector._channel_cache["C99999"] = ChannelConfig(instance="alpha", name="test")
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

        with patch.object(connector, "_download_slack_file", new_callable=AsyncMock) as mock_dl:
            mock_dl.return_value = tmp_path / "data.csv"
            await connector._handle_message(event, AsyncMock())

        mock_service.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_download_skips_oversized_files(self, tmp_path):
        """Files over 50MB are skipped."""
        config = make_config()
        connector = SlackConnector(config, AsyncMock())

        result = await connector._download_slack_file(
            {"name": "huge.zip", "size": 100 * 1024 * 1024, "url_private": "https://example.com"},
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
