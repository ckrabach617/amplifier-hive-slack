"""Slack Socket Mode connector for Amplifier Hive.

This module handles all Slack-specific communication:
- Receives events via Socket Mode (outbound WebSocket, no public URL needed)
- Routes @mentions to the SessionManager
- Posts responses in threads with persona customization (chat:write.customize)

This code stays in amplifier-hive-slack permanently.
It knows about Slack. It knows nothing about Amplifier internals.
It only talks to SessionManager.execute().
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from hive_slack.config import HiveSlackConfig

logger = logging.getLogger(__name__)


class SessionManager(Protocol):
    """Service boundary — same signature as future gRPC SessionService.Execute."""

    async def execute(
        self, instance_name: str, conversation_id: str, prompt: str
    ) -> str: ...


def markdown_to_slack(text: str) -> str:
    """Convert standard markdown to Slack's mrkdwn format.

    Slack's mrkdwn differs from standard markdown:
    - *bold* instead of **bold**
    - <url|text> instead of [text](url)
    - No heading syntax (use bold instead)
    - No table syntax (render as monospace code block)
    - No horizontal rules (render as unicode line)

    Order of operations matters: tables and code blocks are extracted
    first so their content isn't mangled by inline formatting conversions.
    """
    protected: list[str] = []

    def _protect(content: str) -> str:
        protected.append(content)
        return f"\x00PROTECTED{len(protected) - 1}\x00"

    # 1. Protect existing code blocks
    text = re.sub(r"```[\s\S]*?```", lambda m: _protect(m.group(0)), text)

    # 2. Protect inline code
    text = re.sub(r"`[^`]+`", lambda m: _protect(m.group(0)), text)

    # 3. Extract and convert tables BEFORE inline formatting
    #    (so **bold** in cells becomes plain text in the code block)
    text = _convert_tables(text, _protect)

    # 4. Now safe to do inline formatting (tables are protected)
    # Bold: **text** → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # Headings: # Heading → *Heading*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # Horizontal rules: ---, ***, ___ → visual separator with spacing
    text = re.sub(
        r"^[-*_]{3,}\s*$",
        "\n───────────────────────────────\n",
        text,
        flags=re.MULTILINE,
    )

    # 5. Restore all protected content
    for i, content in enumerate(protected):
        text = text.replace(f"\x00PROTECTED{i}\x00", content)

    # Clean up excessive blank lines (3+ → 2)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _convert_tables(text: str, protect_fn) -> str:
    """Find markdown tables and convert to a list format that wraps gracefully.

    Slack has no table support and code blocks break on narrow screens,
    so we render tables as structured lists that reflow naturally.
    """
    lines = text.split("\n")
    result: list[str] = []
    table_lines: list[str] = []
    in_table = False

    for line in lines:
        is_table_row = bool(re.match(r"^\s*\|.*\|\s*$", line))
        is_separator = bool(re.match(r"^\s*\|[-:\s|]+\|\s*$", line))

        if is_table_row:
            if not in_table:
                in_table = True
                table_lines = []
            if not is_separator:
                table_lines.append(line)
        else:
            if in_table:
                result.append(protect_fn(_render_table_as_list(table_lines)))
                table_lines = []
                in_table = False
            result.append(line)

    if in_table:
        result.append(protect_fn(_render_table_as_list(table_lines)))

    return "\n".join(result)


def _clean_cell(text: str) -> str:
    """Strip markdown bold from cell text."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).strip()


def _render_table_as_list(rows: list[str]) -> str:
    """Render a markdown table as a structured list that wraps gracefully.

    Two-column tables become:
        *Key:* Value
        *Key:* Value

    Multi-column tables become:
        *Row Label*
          Col2Header: value
          Col3Header: value
    """
    parsed: list[list[str]] = []
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        parsed.append(cells)

    if not parsed:
        return ""

    headers = parsed[0]
    data_rows = parsed[1:]

    if not data_rows:
        return "  ".join(f"*{_clean_cell(h)}*" for h in headers)

    # Two-column: simple key/value pairs
    if len(headers) == 2:
        lines = []
        for row in data_rows:
            key = _clean_cell(row[0]) if len(row) > 0 else ""
            val = row[1].strip() if len(row) > 1 else ""
            lines.append(f"*{key}:* {val}")
        return "\n".join(lines)

    # Multi-column: use header names as labels per data row
    lines = []
    for row in data_rows:
        row_label = _clean_cell(row[0]) if row else ""
        lines.append(f"*{row_label}*")
        for col_idx in range(1, len(headers)):
            header = _clean_cell(headers[col_idx]) if col_idx < len(headers) else ""
            value = row[col_idx].strip() if col_idx < len(row) else ""
            lines.append(f"  {header}: {value}")
        lines.append("")
    return "\n".join(lines).rstrip()


@dataclass
class ChannelConfig:
    """Parsed routing config from a channel's topic.

    Channel topics can contain [key:value] directives that control routing:
        [instance:alpha]    → all messages routed to alpha
        [mode:roundtable]   → all instances respond
        [default:alpha]     → alpha unless /name override
    """

    instance: str | None = None
    mode: str | None = None
    default: str | None = None


class SlackConnector:
    """Slack Socket Mode listener + response poster.

    Listens for @mentions and channel messages via Socket Mode,
    routes to SessionManager based on channel topic configuration,
    posts responses in threads with persona customization.
    """

    def __init__(self, config: HiveSlackConfig, service: SessionManager) -> None:
        self._config = config
        self._service = service
        self._app = AsyncApp(token=config.slack.bot_token)
        self._handler = AsyncSocketModeHandler(self._app, config.slack.app_token)

        # Bot user ID — populated in start() via auth.test
        self._bot_user_id: str = ""

        # Channel topic config cache (avoids hitting conversations.info every message)
        self._channel_cache: dict[str, ChannelConfig] = {}
        self._cache_timestamps: dict[str, float] = {}
        self._cache_ttl = 60  # seconds — re-read topic every 60s

        # Register event handlers
        self._app.event("app_mention")(self._handle_mention)
        self._app.event("message")(self._handle_message)

    async def _handle_mention(self, event: dict, say) -> None:
        """Handle @mention events — the core message flow."""
        text = self._strip_mention(event.get("text", ""))
        if not text:
            return

        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user = event.get("user", "unknown")

        # Route to instance: parse /name prefix or use default
        instance_name, prompt = self._parse_instance_prefix(
            text, self._config.instance_names, self._config.default_instance
        )
        instance = self._config.get_instance(instance_name)

        conversation_id = f"{channel}:{thread_ts}"

        logger.info(
            "Mention from %s → %s in %s: %s",
            user,
            instance_name,
            conversation_id,
            prompt[:100],
        )

        try:
            response = await self._service.execute(
                instance_name,
                conversation_id,
                prompt,
            )

            await say(
                text=markdown_to_slack(response),
                thread_ts=thread_ts,
                username=instance.persona.name,
                icon_emoji=instance.persona.emoji,
            )
        except Exception:
            logger.exception("Error handling mention in %s", conversation_id)
            await say(
                text="Sorry, I encountered an error processing your request.",
                thread_ts=thread_ts,
                username=instance.persona.name,
                icon_emoji=instance.persona.emoji,
            )

    @staticmethod
    def _strip_mention(text: str) -> str:
        """Remove <@U12345> mention prefix from message text."""
        return re.sub(r"^<@[A-Z0-9]+>\s*", "", text).strip()

    @staticmethod
    def _parse_instance_prefix(
        text: str,
        known_instances: list[str],
        default: str,
    ) -> tuple[str, str]:
        """Parse /instance-name prefix from message text.

        Returns (instance_name, remaining_text).

        Examples:
            "/alpha review this code" → ("alpha", "review this code")
            "/beta what do you think" → ("beta", "what do you think")
            "just a question" → (default, "just a question")
            "/unknown hello" → (default, "/unknown hello")  # unknown name kept as text
        """
        match = re.match(r"^/(\w+)\s+(.*)", text, re.DOTALL)
        if match:
            candidate = match.group(1).lower()
            if candidate in known_instances:
                return candidate, match.group(2).strip()
        return default, text

    async def _handle_message(self, event: dict, say) -> None:
        """Handle all channel messages — the primary routing path for configured channels.

        This handler fires on ALL channel messages (not just @mentions).
        It requires these Slack app event subscriptions:
            - message.channels  (messages in public channels)
            - message.groups    (messages in private channels)

        And these Bot Token Scopes:
            - channels:read     (conversations.info for channel topics)
            - groups:read       (same for private channels)
            - channels:history  (receive message events in public channels)
            - groups:history    (receive message events in private channels)

        Routing is determined by [key:value] directives in the channel topic.
        Unconfigured channels are ignored (backward compatible — @mention still works).
        """
        # Skip bot messages (prevent loops!)
        if event.get("bot_id") or event.get("subtype"):
            return

        # Skip if this is an @mention (handled by _handle_mention)
        if self._bot_user_id and f"<@{self._bot_user_id}>" in event.get("text", ""):
            return

        text = event.get("text", "").strip()
        if not text:
            return

        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user = event.get("user", "unknown")

        # Get channel config from topic
        channel_config = await self._get_channel_config(channel)

        # Route based on config
        if channel_config.instance:
            # Single-instance channel: all messages go to this instance
            instance_name = channel_config.instance
            prompt = text
        elif channel_config.mode == "roundtable":
            # TODO: Milestone 4 — fan out to all instances
            # For now, treat as default routing
            instance_name = self._config.default_instance
            prompt = text
        elif channel_config.default:
            # Default instance channel: check for /name override, else use default
            instance_name, prompt = self._parse_instance_prefix(
                text, self._config.instance_names, channel_config.default
            )
        else:
            # Unconfigured channel: ignore non-mention messages
            return

        # Verify instance exists
        try:
            instance = self._config.get_instance(instance_name)
        except KeyError:
            logger.warning("Unknown instance '%s' in channel config", instance_name)
            return

        conversation_id = f"{channel}:{thread_ts}"

        logger.info(
            "Message from %s → %s in %s: %s",
            user,
            instance_name,
            conversation_id,
            text[:100],
        )

        try:
            response = await self._service.execute(
                instance_name,
                conversation_id,
                prompt,
            )

            await say(
                text=markdown_to_slack(response),
                thread_ts=thread_ts,
                username=instance.persona.name,
                icon_emoji=instance.persona.emoji,
            )
        except Exception:
            logger.exception("Error handling message in %s", conversation_id)
            await say(
                text="Something's not working on my end. Try again?",
                thread_ts=thread_ts,
                username=instance.persona.name,
                icon_emoji=instance.persona.emoji,
            )

    async def _get_channel_config(self, channel_id: str) -> ChannelConfig:
        """Get routing config for a channel, parsed from its topic. Cached."""
        now = time.time()
        if (
            channel_id in self._channel_cache
            and now - self._cache_timestamps.get(channel_id, 0) < self._cache_ttl
        ):
            return self._channel_cache[channel_id]

        # Fetch channel info from Slack API
        try:
            result = await self._app.client.conversations_info(channel=channel_id)
            topic = result.get("channel", {}).get("topic", {}).get("value", "")
        except Exception:
            logger.warning("Could not fetch channel info for %s", channel_id)
            topic = ""

        config = self._parse_channel_topic(topic, self._config.instance_names)
        self._channel_cache[channel_id] = config
        self._cache_timestamps[channel_id] = now

        logger.debug("Channel %s config: %s (topic: %s)", channel_id, config, topic)
        return config

    @staticmethod
    def _parse_channel_topic(topic: str, known_instances: list[str]) -> ChannelConfig:
        """Parse [key:value] routing directives from a channel topic.

        Supports:
            [instance:alpha]    → all messages to alpha
            [mode:roundtable]   → all instances respond
            [default:alpha]     → alpha unless /name override

        Unknown instance names in directives are ignored.
        """
        config = ChannelConfig()

        for match in re.finditer(r"\[(\w+):(\w+)\]", topic):
            key = match.group(1).lower()
            value = match.group(2).lower()

            if key == "instance" and value in known_instances:
                config.instance = value
            elif key == "mode" and value in ("roundtable", "open"):
                config.mode = value
            elif key == "default" and value in known_instances:
                config.default = value

        return config

    async def start(self) -> None:
        """Start the Socket Mode handler (blocks until stopped)."""
        logger.info("Starting Slack Socket Mode connection...")

        # Get our own bot user ID for filtering @mentions in _handle_message
        try:
            auth = await self._app.client.auth_test()
            self._bot_user_id = auth.get("user_id", "")
            logger.info("Bot user ID: %s", self._bot_user_id)
        except Exception:
            logger.warning("Could not determine bot user ID")
            self._bot_user_id = ""

        await self._handler.start_async()

    async def stop(self) -> None:
        """Stop the Socket Mode handler."""
        logger.info("Stopping Slack connector...")
        await self._handler.close_async()
