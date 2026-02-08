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
from pathlib import Path
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
    name: str = ""  # Channel name for context enrichment


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
        self._bot_id: str = ""  # The bot's bot_id (different from user_id)

        # Channel topic config cache (avoids hitting conversations.info every message)
        self._channel_cache: dict[str, ChannelConfig] = {}
        self._cache_timestamps: dict[str, float] = {}
        self._cache_ttl = 60  # seconds — re-read topic every 60s

        # Track messages we've already handled (prevent double-processing)
        self._handled_messages: set[str] = set()

        # Track prompts that generated bot responses (for reaction commands)
        # message_ts → (instance_name, conversation_id, prompt)
        self._message_prompts: dict[str, tuple[str, str, str]] = {}

        # Register event handlers
        self._app.event("app_mention")(self._handle_mention)
        self._app.event("message")(self._handle_message)
        # Requires Slack app scopes: reactions:read, reactions:write
        # Requires event subscription: reaction_added
        self._app.event("reaction_added")(self._handle_reaction)

    def _build_prompt(
        self,
        text: str,
        user: str,
        channel: str,
        channel_name: str = "",
        file_descriptions: str | None = None,
    ) -> str:
        """Enrich the raw message with context about who/where."""
        parts = []
        if channel_name:
            parts.append(f"[From <@{user}> in #{channel_name}]")
        else:
            parts.append(f"[DM from <@{user}>]")
        if file_descriptions:
            parts.append(file_descriptions)
        parts.append("[To share files back, copy them to .outbox/ in your working directory]")
        if text:
            parts.append(text)
        return "\n".join(parts)

    async def _download_slack_file(
        self, file_info: dict, working_dir: Path
    ) -> Path | None:
        """Download a single file from Slack to the instance's working directory.

        Returns the local path where the file was saved, or None on failure.
        Max file size: 50MB.
        """
        MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

        url = file_info.get("url_private")
        name = file_info.get("name", "unknown")
        size = file_info.get("size", 0)

        if not url:
            logger.warning("File %s has no url_private, skipping", name)
            return None

        if size > MAX_FILE_SIZE:
            logger.warning("File %s too large (%d bytes), skipping", name, size)
            return None

        # Sanitize filename
        safe_name = re.sub(r"[^\w\-.]", "_", name)
        if not safe_name:
            safe_name = "uploaded_file"

        # Handle filename conflicts
        dest = working_dir / safe_name
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 1
            while dest.exists():
                dest = working_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._config.slack.bot_token}"
                    },
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Failed to download %s: HTTP %d", name, resp.status
                        )
                        return None
                    content = await resp.read()
                    dest.write_bytes(content)
                    logger.info(
                        "Downloaded %s (%d bytes) to %s", name, len(content), dest
                    )
                    return dest
        except Exception:
            logger.exception("Error downloading file %s", name)
            return None

    async def _process_outbox(
        self,
        working_dir: Path,
        channel: str,
        thread_ts: str,
        instance: object,
    ) -> None:
        """Check .outbox/ for files to share back to Slack.

        Files are uploaded to the Slack thread and deleted from .outbox/ on success.
        Failures are logged but don't crash the handler.
        """
        outbox = working_dir / ".outbox"
        if not outbox.exists() or not outbox.is_dir():
            return

        for filepath in sorted(outbox.iterdir()):
            if filepath.name.startswith(".") or filepath.is_dir():
                continue

            try:
                await self._app.client.files_upload_v2(
                    channel=channel,
                    thread_ts=thread_ts,
                    file=str(filepath),
                    title=filepath.name,
                    initial_comment=f"📎 {filepath.name}",
                )
                filepath.unlink()
                logger.info(
                    "Shared %s to Slack and removed from outbox", filepath.name
                )
            except Exception:
                logger.warning(
                    "Failed to upload %s to Slack", filepath.name, exc_info=True
                )

    async def _handle_mention(self, event: dict, say) -> None:
        """Handle @mention events — the core message flow."""
        # Mark this message as handled so _handle_message skips it
        msg_ts = event.get("ts", "")
        self._handled_messages.add(msg_ts)
        # Keep the set bounded (only recent messages matter)
        if len(self._handled_messages) > 1000:
            self._handled_messages = set(list(self._handled_messages)[-500:])

        text = self._strip_mention(event.get("text", ""))
        if not text:
            return

        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user = event.get("user", "unknown")

        # Route to instance: parse name prefix or use default
        instance_name, prompt = self._parse_instance_prefix(
            text, self._config.instance_names, self._config.default_instance
        )
        instance = self._config.get_instance(instance_name)

        conversation_id = f"{channel}:{thread_ts}"

        # Download any uploaded files (mentions can include files too)
        files = event.get("files", [])
        file_descriptions = None
        if files:
            working_dir = Path(instance.working_dir).expanduser()
            working_dir.mkdir(parents=True, exist_ok=True)
            desc_lines = []
            for file_info in files:
                saved_path = await self._download_slack_file(file_info, working_dir)
                if saved_path:
                    desc_lines.append(
                        f"  {file_info.get('name', 'file')} ({file_info.get('size', 0)} bytes) → ./{saved_path.name}"
                    )
            if desc_lines:
                file_descriptions = (
                    "[User uploaded files:\n" + "\n".join(desc_lines) + "]"
                )

        # Get channel name for context enrichment
        channel_config = await self._get_channel_config(channel)
        prompt = self._build_prompt(
            prompt, user, channel, channel_config.name, file_descriptions
        )

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

            # Check outbox for files to share back
            working_dir = Path(instance.working_dir).expanduser()
            await self._process_outbox(working_dir, channel, thread_ts, instance)

            result = await say(
                text=markdown_to_slack(response),
                thread_ts=thread_ts,
                username=instance.persona.name,
                icon_emoji=instance.persona.emoji,
            )
            self._track_prompt(result, instance_name, conversation_id, prompt)
        except Exception:
            logger.exception("Error handling mention in %s", conversation_id)
            await say(
                text="Something's not working on my end. Try again?",
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
        """Parse instance name from the start of message text.

        Returns (instance_name, remaining_text).

        Supports natural addressing patterns:
            "alpha: review this code"     → ("alpha", "review this code")
            "alpha, what do you think"    → ("alpha", "what do you think")
            "@alpha review this"          → ("alpha", "review this")
            "alpha review this"           → ("alpha", "review this")
            "hey alpha, look at this"     → ("alpha", "look at this")
            "just a question"             → (default, "just a question")
            "the alpha version is..."     → (default, "the alpha version is...")
        """
        lower = text.lower()

        # Pattern 1: "name: ..." or "name, ..."
        match = re.match(r"^(\w+)[,:]\s+(.*)", text, re.DOTALL)
        if match and match.group(1).lower() in known_instances:
            return match.group(1).lower(), match.group(2).strip()

        # Pattern 2: "@name ..."
        match = re.match(r"^@(\w+)\s+(.*)", text, re.DOTALL)
        if match and match.group(1).lower() in known_instances:
            return match.group(1).lower(), match.group(2).strip()

        # Pattern 3: "hey name, ..." or "hey name ..."
        match = re.match(r"^hey\s+(\w+)[,\s]+(.*)", text, re.DOTALL | re.IGNORECASE)
        if match and match.group(1).lower() in known_instances:
            return match.group(1).lower(), match.group(2).strip()

        # Pattern 4: "name ..." (name as first word, only if unambiguous)
        # Only match if the first word IS an instance name exactly
        first_word = lower.split()[0] if lower.split() else ""
        if first_word in known_instances:
            rest = text[len(first_word) :].strip()
            if rest:
                return first_word, rest

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
        logger.debug(
            "Message event received: channel=%s user=%s bot_id=%s subtype=%s text=%s",
            event.get("channel"),
            event.get("user"),
            event.get("bot_id"),
            event.get("subtype"),
            (event.get("text", ""))[:50],
        )

        # Skip bot messages (prevent loops!)
        if event.get("bot_id"):
            logger.debug("Skipping: bot_id=%s", event.get("bot_id"))
            return
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            logger.debug("Skipping: subtype=%s", subtype)
            return

        # Skip messages from our own bot user (belt + suspenders for loop prevention)
        if event.get("user") == self._bot_user_id:
            logger.debug("Skipping: message from our own bot user")
            return

        # Skip if already handled by _handle_mention (prevents double-processing)
        msg_ts = event.get("ts", "")
        if msg_ts in self._handled_messages:
            logger.debug("Skipping: already handled by _handle_mention (ts=%s)", msg_ts)
            return

        # Skip if this is an @mention (handled by _handle_mention)
        if self._bot_user_id and f"<@{self._bot_user_id}>" in event.get("text", ""):
            logger.debug("Skipping: contains bot @mention (handled by _handle_mention)")
            return

        text = event.get("text", "").strip()
        files = event.get("files", [])
        if not text and not files:
            return

        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user = event.get("user", "unknown")
        channel_type = event.get("channel_type", "")

        if channel_type == "im":
            # DM: use natural addressing or default instance, no topic needed.
            # Requires Slack app scopes: im:read, im:history
            # Requires event subscription: message.im
            instance_name, prompt = self._parse_instance_prefix(
                text, self._config.instance_names, self._config.default_instance
            )
            conversation_id = f"dm:{user}"
            channel_name = ""  # Will produce DM context in _build_prompt
        else:
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

            conversation_id = f"{channel}:{thread_ts}"
            channel_name = channel_config.name

        # Verify instance exists
        try:
            instance = self._config.get_instance(instance_name)
        except KeyError:
            logger.warning("Unknown instance '%s' in channel config", instance_name)
            return

        # Download any uploaded files
        file_descriptions = None
        if files:
            working_dir = Path(instance.working_dir).expanduser()
            working_dir.mkdir(parents=True, exist_ok=True)
            desc_lines = []
            for file_info in files:
                saved_path = await self._download_slack_file(file_info, working_dir)
                if saved_path:
                    size = file_info.get("size", 0)
                    desc_lines.append(
                        f"  {file_info.get('name', 'file')} ({size} bytes) → ./{saved_path.name}"
                    )
            if desc_lines:
                file_descriptions = (
                    "[User uploaded files:\n" + "\n".join(desc_lines) + "]"
                )

        # Enrich prompt with context
        prompt = self._build_prompt(
            prompt, user, channel, channel_name, file_descriptions
        )

        logger.info(
            "Message from %s → %s in %s: %s",
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

            # Check outbox for files to share back
            working_dir = Path(instance.working_dir).expanduser()
            await self._process_outbox(working_dir, channel, thread_ts, instance)

            result = await say(
                text=markdown_to_slack(response),
                thread_ts=thread_ts,
                username=instance.persona.name,
                icon_emoji=instance.persona.emoji,
            )
            self._track_prompt(result, instance_name, conversation_id, prompt)
        except Exception:
            logger.exception("Error handling message in %s", conversation_id)
            await say(
                text="Something's not working on my end. Try again?",
                thread_ts=thread_ts,
                username=instance.persona.name,
                icon_emoji=instance.persona.emoji,
            )

    def _track_prompt(
        self,
        say_result: object,
        instance_name: str,
        conversation_id: str,
        prompt: str,
    ) -> None:
        """Track the prompt that generated a bot response for reaction commands."""
        if isinstance(say_result, dict) and say_result.get("ts"):
            self._message_prompts[say_result["ts"]] = (
                instance_name,
                conversation_id,
                prompt,
            )
            # Keep bounded (last 500 entries)
            if len(self._message_prompts) > 500:
                oldest_keys = list(self._message_prompts.keys())[:-500]
                for key in oldest_keys:
                    del self._message_prompts[key]

    async def _handle_reaction(self, event: dict, say) -> None:
        """Handle emoji reactions on bot messages.

        Supported reactions:
            🔄 repeat / arrows_counterclockwise → Regenerate response
            ❌ x → Cancel acknowledgment (full cancellation needs coordinator)

        Requires Slack app scopes: reactions:read, reactions:write
        Requires event subscription: reaction_added
        """
        reaction = event.get("reaction", "")
        item = event.get("item", {})
        channel = item.get("channel", "")
        message_ts = item.get("ts", "")
        user = event.get("user", "")

        # Only handle reactions on our bot's messages
        if message_ts not in self._message_prompts:
            return

        if reaction in ("repeat", "arrows_counterclockwise"):
            # Regenerate: re-execute the original prompt
            instance_name, conversation_id, original_prompt = self._message_prompts[
                message_ts
            ]
            instance = self._config.get_instance(instance_name)

            logger.info("Regenerate requested by %s for %s", user, message_ts)

            try:
                response = await self._service.execute(
                    instance_name,
                    conversation_id,
                    original_prompt,
                )
                await self._app.client.chat_postMessage(
                    channel=channel,
                    text=markdown_to_slack(response),
                    thread_ts=message_ts,
                    username=instance.persona.name,
                    icon_emoji=instance.persona.emoji,
                )
            except Exception:
                logger.exception("Error regenerating response")

        elif reaction == "x":
            logger.info("Cancel requested by %s for %s", user, message_ts)
            # For now, just acknowledge. Full cancellation needs coordinator integration.
            await self._app.client.reactions_add(
                channel=channel,
                timestamp=message_ts,
                name="white_check_mark",
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
        channel_name = ""
        try:
            result = await self._app.client.conversations_info(channel=channel_id)
            channel_data = result.get("channel", {})
            topic = channel_data.get("topic", {}).get("value", "")
            channel_name = channel_data.get("name", "")
        except Exception:
            logger.warning("Could not fetch channel info for %s", channel_id)
            topic = ""

        config = self._parse_channel_topic(topic, self._config.instance_names)
        config.name = channel_name
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
