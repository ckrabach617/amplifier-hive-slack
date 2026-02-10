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
from typing import Any, Awaitable, Callable, Protocol

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from hive_slack.config import HiveSlackConfig

logger = logging.getLogger(__name__)


class SessionManager(Protocol):
    """Service boundary — same signature as future gRPC SessionService.Execute."""

    async def execute(
        self,
        instance_name: str,
        conversation_id: str,
        prompt: str,
        on_progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        slack_context: dict[str, Any] | None = None,
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


def _friendly_tool_name(tool_name: str) -> str:
    """Convert tool module names to human-friendly descriptions."""
    friendly = {
        "read_file": "Reading files",
        "write_file": "Writing files",
        "edit_file": "Editing files",
        "bash": "Running command",
        "glob": "Searching files",
        "grep": "Searching content",
        "web_search": "Searching the web",
        "web_fetch": "Fetching web page",
        "delegate": "Delegating to agent",
        "todo": "Managing tasks",
        "LSP": "Analyzing code",
        "python_check": "Checking code quality",
        "load_skill": "Loading knowledge",
        "recipes": "Running recipe",
    }
    return friendly.get(tool_name, f"Working ({tool_name})")


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

        # Active execution tracking for progress indicators
        # conversation_id → {"status_ts", "user_ts", "channel", "thread_ts", "instance_name"}
        self._active_executions: dict[str, dict] = {}
        # Message queuing for conversations with active executions
        # conversation_id → [queued prompts]
        self._message_queues: dict[str, list[str]] = {}

        # Thread ownership: conversation_id → instance_name (or "_ROUNDTABLE")
        self._thread_owners: dict[str, str] = {}
        self._thread_owner_order: list[str] = []
        _THREAD_OWNER_LIMIT = 10_000

        # Register event handlers
        self._app.event("app_mention")(self._handle_mention)
        self._app.event("message")(self._handle_message)
        # Requires Slack app scopes: reactions:read, reactions:write
        # Requires event subscription: reaction_added
        self._app.event("reaction_added")(self._handle_reaction)

        # Handle Block Kit button clicks (for approval system)
        import re as _re

        self._app.action(_re.compile(r"^approval_"))(self._handle_approval_action)

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
        parts.append(
            "[To share files back, copy them to .outbox/ in your working directory]"
        )
        if text:
            parts.append(text)
        return "\n".join(parts)

    async def _send_welcome_dm(self, user_id: str, persona) -> None:
        """Send one-time welcome DM to a first-time user."""
        try:
            result = await self._app.client.conversations_open(users=user_id)
            dm_channel = result["channel"]["id"]

            welcome = (
                f"Hey \u2014 I'm {persona.name}. Since this is your first time, "
                "one thing worth knowing:\n\n"
                "Each thread is its own conversation. I start fresh every "
                "time, so I won't have context from other threads. If you "
                "need to reference something from elsewhere, just paste "
                "the relevant bit.\n\n"
                "You can @mention me in channels or message me here directly."
            )

            await self._app.client.chat_postMessage(
                channel=dm_channel,
                text=welcome,
                username=persona.name,
                icon_emoji=persona.emoji,
            )
            logger.info("Sent welcome DM to %s", user_id)
        except Exception:
            logger.warning("Failed to send welcome DM to %s", user_id, exc_info=True)

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
                    headers={"Authorization": f"Bearer {self._config.slack.bot_token}"},
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
                logger.info("Shared %s to Slack and removed from outbox", filepath.name)
            except Exception:
                logger.warning(
                    "Failed to upload %s to Slack", filepath.name, exc_info=True
                )

    async def _execute_with_progress(
        self,
        instance_name: str,
        instance,  # InstanceConfig
        conversation_id: str,
        prompt: str,
        channel: str,
        thread_ts: str,
        user_ts: str,
        say,
        *,
        onboarding: object | None = None,
        is_new_thread: bool = False,
        has_cross_ref: bool = False,
    ) -> None:
        """Execute a prompt with progress indicators and message queuing."""
        import time as _time

        start_time = _time.monotonic()

        # React with ⏳ on the user's message
        try:
            await self._app.client.reactions_add(
                channel=channel,
                timestamp=user_ts,
                name="hourglass_flowing_sand",
            )
        except Exception:
            pass  # Best effort

        # Post editable status message (bot's own identity, NOT persona)
        status_msg = None
        try:
            result = await self._app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="\u2699\ufe0f Working...",
            )
            status_msg = result.get("ts")
        except Exception:
            logger.debug("Could not post status message")

        # Track this execution
        self._active_executions[conversation_id] = {
            "status_ts": status_msg,
            "user_ts": user_ts,
            "channel": channel,
            "thread_ts": thread_ts,
            "instance_name": instance_name,
        }

        # Progress callback for service.execute()
        async def on_progress(event_type: str, data: dict) -> None:
            if not status_msg:
                return
            text = None
            if event_type == "executing":
                text = "\u2699\ufe0f Working..."
            elif event_type in ("tool:pre", "tool:start"):
                tool = data.get("tool", data.get("tool_name", ""))
                friendly = _friendly_tool_name(tool)
                text = f"\u2699\ufe0f {friendly}..."
            elif event_type in ("tool:post", "tool:end"):
                tool = data.get("tool", data.get("tool_name", ""))
                friendly = _friendly_tool_name(tool)
                text = f"\u2699\ufe0f {friendly} done. Thinking..."
            elif event_type in ("complete", "error"):
                return  # We handle completion below

            if text:
                queued = len(self._message_queues.get(conversation_id, []))
                if queued:
                    text += f" ({queued} message{'s' if queued != 1 else ''} queued)"
                try:
                    await self._app.client.chat_update(
                        channel=channel,
                        ts=status_msg,
                        text=text,
                    )
                except Exception:
                    pass  # Best effort, may hit rate limits

        # Build Slack context for session creation
        slack_context = {
            "client": self._app.client,
            "channel": channel,
            "thread_ts": thread_ts,
            "user_ts": user_ts,
        }

        try:
            # Execute
            response = await self._service.execute(
                instance_name,
                conversation_id,
                prompt,
                on_progress=on_progress,
                slack_context=slack_context,
            )

            # Delete status message
            if status_msg:
                try:
                    await self._app.client.chat_delete(
                        channel=channel,
                        ts=status_msg,
                    )
                except Exception:
                    pass

            # Process outbox (file sharing)
            working_dir = Path(instance.working_dir).expanduser()
            await self._process_outbox(working_dir, channel, thread_ts, instance)

            # Post final response with persona
            response_text = markdown_to_slack(response)

            # Append onboarding suffix if applicable
            if onboarding and hasattr(onboarding, "get_response_suffix"):
                import asyncio as _asyncio

                duration = _time.monotonic() - start_time
                suffix = onboarding.get_response_suffix(
                    is_new_thread, duration, has_cross_ref
                )
                if suffix:
                    response_text = f"{response_text}\n{suffix}"
                # Fire-and-forget save
                _asyncio.create_task(onboarding.save())

            result = await say(
                text=response_text,
                thread_ts=thread_ts,
                username=instance.persona.name,
                icon_emoji=instance.persona.emoji,
            )
            self._track_prompt(result, instance_name, conversation_id, prompt)
            self._set_thread_owner(conversation_id, instance_name)

        except Exception:
            logger.exception("Error in execution for %s", conversation_id)
            # Delete status message on error too
            if status_msg:
                try:
                    await self._app.client.chat_delete(
                        channel=channel,
                        ts=status_msg,
                    )
                except Exception:
                    pass
            await say(
                text="Something's not working on my end. Try again?",
                thread_ts=thread_ts,
                username=instance.persona.name,
                icon_emoji=instance.persona.emoji,
            )
        finally:
            # Remove ⏳ reaction
            try:
                await self._app.client.reactions_remove(
                    channel=channel,
                    timestamp=user_ts,
                    name="hourglass_flowing_sand",
                )
            except Exception:
                pass

            # Clear active execution
            self._active_executions.pop(conversation_id, None)

            # Process queued messages
            queued = self._message_queues.pop(conversation_id, [])
            if queued:
                combined = "\n".join(f"- {m}" for m in queued)
                batch_prompt = (
                    "[You completed a previous task. The user sent additional "
                    "messages while you were working. Please address these:]\n"
                    f"{combined}"
                )
                # Recursively execute the batch (will get its own status message)
                await self._execute_with_progress(
                    instance_name,
                    instance,
                    conversation_id,
                    batch_prompt,
                    channel,
                    thread_ts,
                    user_ts,
                    say,
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
        instance_name, prompt, _ = self._parse_instance_prefix(
            text, self._config.instance_names, self._config.default_instance
        )
        instance = self._config.get_instance(instance_name)

        conversation_id = f"{channel}:{thread_ts}"

        # Onboarding
        from hive_slack.onboarding import UserOnboarding

        onboarding = await UserOnboarding.load(user)
        if onboarding.is_first_interaction:
            await self._send_welcome_dm(user, instance.persona)
            onboarding.mark_welcomed()

        is_new_thread = onboarding.record_thread(conversation_id)
        has_cross_ref = (
            UserOnboarding.has_cross_thread_reference(text) if is_new_thread else False
        )

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

        # Check if this conversation is already executing
        if conversation_id in self._active_executions:
            # Try to inject into the running orchestrator (mid-execution steering)
            exec_info = self._active_executions[conversation_id]
            injected = (
                self._service.inject_message(
                    exec_info.get("instance_name", instance_name),
                    conversation_id,
                    prompt,
                )
                if hasattr(self._service, "inject_message")
                else False
            )

            if not injected:
                # Fallback: queue locally for batch after execution
                self._message_queues.setdefault(conversation_id, []).append(prompt)

            # Acknowledge with 📨 either way
            try:
                await self._app.client.reactions_add(
                    channel=channel,
                    timestamp=event.get("ts", ""),
                    name="incoming_envelope",
                )
            except Exception:
                pass
            logger.info(
                "%s message for busy conversation %s",
                "Injected" if injected else "Queued",
                conversation_id,
            )
            return

        await self._execute_with_progress(
            instance_name,
            instance,
            conversation_id,
            prompt,
            channel,
            thread_ts,
            event.get("ts", ""),
            say,
            onboarding=onboarding,
            is_new_thread=is_new_thread,
            has_cross_ref=has_cross_ref,
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
    ) -> tuple[str, str, bool]:
        """Parse instance name from the start of message text.

        Returns (instance_name, remaining_text, was_explicit).
        was_explicit is True if a pattern matched, False if fell through to default.

        Supports natural addressing patterns:
            "alpha: review this code"     → ("alpha", "review this code", True)
            "alpha, what do you think"    → ("alpha", "what do you think", True)
            "@alpha review this"          → ("alpha", "review this", True)
            "alpha review this"           → ("alpha", "review this", True)
            "hey alpha, look at this"     → ("alpha", "look at this", True)
            "just a question"             → (default, "just a question", False)
            "the alpha version is..."     → (default, "the alpha version is...", False)
        """
        lower = text.lower()

        # Pattern 1: "name: ..." or "name, ..."
        match = re.match(r"^(\w+)[,:]\s+(.*)", text, re.DOTALL)
        if match and match.group(1).lower() in known_instances:
            return match.group(1).lower(), match.group(2).strip(), True

        # Pattern 2: "@name ..."
        match = re.match(r"^@(\w+)\s+(.*)", text, re.DOTALL)
        if match and match.group(1).lower() in known_instances:
            return match.group(1).lower(), match.group(2).strip(), True

        # Pattern 3: "hey name, ..." or "hey name ..."
        match = re.match(r"^hey\s+(\w+)[,\s]+(.*)", text, re.DOTALL | re.IGNORECASE)
        if match and match.group(1).lower() in known_instances:
            return match.group(1).lower(), match.group(2).strip(), True

        # Pattern 4: "name ..." (name as first word, only if unambiguous)
        # Only match if the first word IS an instance name exactly
        first_word = lower.split()[0] if lower.split() else ""
        if first_word in known_instances:
            rest = text[len(first_word) :].strip()
            if rest:
                return first_word, rest, True

        return default, text, False

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
            instance_name, prompt, _ = self._parse_instance_prefix(
                text, self._config.instance_names, self._config.default_instance
            )
            conversation_id = f"dm:{user}"
            channel_name = ""  # Will produce DM context in _build_prompt
        else:
            # Get channel config from topic
            channel_config = await self._get_channel_config(channel)
            conversation_id = f"{channel}:{thread_ts}"
            channel_name = channel_config.name

            # Parse for explicit addressing
            addressed_name, addressed_prompt, was_explicit = (
                self._parse_instance_prefix(
                    text, self._config.instance_names, self._config.default_instance
                )
            )
            owner = self._get_thread_owner(conversation_id)

            # Routing priority:
            # 1. Roundtable + unaddressed → fan out
            # 2. Single-instance channel → forced routing
            # 3. Explicit address → that instance
            # 4. Thread owner → owner
            # 5. Channel config (default)
            # 6. No config → ignore

            if channel_config.mode == "roundtable" and not was_explicit:
                # Roundtable fan-out (unaddressed message)
                # Download files first
                file_descriptions = None
                if files:
                    working_dir_path = Path(
                        self._config.get_instance(
                            self._config.default_instance
                        ).working_dir
                    ).expanduser()
                    working_dir_path.mkdir(parents=True, exist_ok=True)
                    desc_lines = []
                    for file_info in files:
                        saved_path = await self._download_slack_file(
                            file_info, working_dir_path
                        )
                        if saved_path:
                            desc_lines.append(
                                f"  {file_info.get('name', 'file')} ({file_info.get('size', 0)} bytes) → ./{saved_path.name}"
                            )
                    if desc_lines:
                        file_descriptions = (
                            "[User uploaded files:\n" + "\n".join(desc_lines) + "]"
                        )

                rt_prompt = self._build_prompt(
                    text, user, channel, channel_name, file_descriptions
                )

                # Onboarding
                from hive_slack.onboarding import UserOnboarding

                onboarding = await UserOnboarding.load(user)
                if onboarding.is_first_interaction:
                    await self._send_welcome_dm(
                        user,
                        self._config.get_instance(
                            self._config.default_instance
                        ).persona,
                    )
                    onboarding.mark_welcomed()
                is_new_thread = onboarding.record_thread(conversation_id)

                await self._execute_roundtable(
                    conversation_id,
                    rt_prompt,
                    channel,
                    thread_ts,
                    event.get("ts", ""),
                    say,
                    onboarding=onboarding,
                    is_new_thread=is_new_thread,
                )
                return

            elif channel_config.mode == "roundtable" and was_explicit:
                # Explicitly addressed in roundtable → single instance
                instance_name = addressed_name
                prompt = addressed_prompt
            elif channel_config.instance:
                # Single-instance channel: all messages go to this instance
                instance_name = channel_config.instance
                prompt = text
            elif was_explicit:
                # User explicitly addressed an instance
                instance_name = addressed_name
                prompt = addressed_prompt
            elif owner and owner != "_ROUNDTABLE":
                # Thread has an owner, no explicit override → route to owner
                instance_name = owner
                prompt = text
            elif channel_config.default:
                # Channel has a default, use it
                instance_name = channel_config.default
                prompt = text
            else:
                # Unconfigured channel: ignore non-mention messages
                return

        # Verify instance exists
        try:
            instance = self._config.get_instance(instance_name)
        except KeyError:
            logger.warning("Unknown instance '%s' in channel config", instance_name)
            return

        # Onboarding
        from hive_slack.onboarding import UserOnboarding

        onboarding = await UserOnboarding.load(user)
        if onboarding.is_first_interaction:
            await self._send_welcome_dm(user, instance.persona)
            onboarding.mark_welcomed()

        is_new_thread = onboarding.record_thread(conversation_id)
        has_cross_ref = (
            UserOnboarding.has_cross_thread_reference(text) if is_new_thread else False
        )

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

        # Check if this conversation is already executing
        if conversation_id in self._active_executions:
            # Try to inject into the running orchestrator (mid-execution steering)
            exec_info = self._active_executions[conversation_id]
            injected = (
                self._service.inject_message(
                    exec_info.get("instance_name", instance_name),
                    conversation_id,
                    prompt,
                )
                if hasattr(self._service, "inject_message")
                else False
            )

            if not injected:
                # Fallback: queue locally for batch after execution
                self._message_queues.setdefault(conversation_id, []).append(prompt)

            # Acknowledge with 📨 either way
            try:
                await self._app.client.reactions_add(
                    channel=channel,
                    timestamp=event.get("ts", ""),
                    name="incoming_envelope",
                )
            except Exception:
                pass
            logger.info(
                "%s message for busy conversation %s",
                "Injected" if injected else "Queued",
                conversation_id,
            )
            return

        await self._execute_with_progress(
            instance_name,
            instance,
            conversation_id,
            prompt,
            channel,
            thread_ts,
            event.get("ts", ""),
            say,
            onboarding=onboarding,
            is_new_thread=is_new_thread,
            has_cross_ref=has_cross_ref,
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

    def _set_thread_owner(self, conversation_id: str, instance_name: str) -> None:
        """Record or transfer thread ownership."""
        if conversation_id in self._thread_owners:
            try:
                self._thread_owner_order.remove(conversation_id)
            except ValueError:
                pass
        self._thread_owners[conversation_id] = instance_name
        self._thread_owner_order.append(conversation_id)
        # Evict oldest if over limit
        while len(self._thread_owners) > 10_000:
            oldest = self._thread_owner_order.pop(0)
            self._thread_owners.pop(oldest, None)

    def _get_thread_owner(self, conversation_id: str) -> str | None:
        """Get the instance that owns this thread, or None."""
        return self._thread_owners.get(conversation_id)

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

        # Emoji summoning: react with an instance-name emoji to summon that instance
        if reaction in self._config.instance_names:
            if user == self._bot_user_id:
                return
            await self._handle_emoji_summon(reaction, channel, message_ts, user, say)
            return

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

    async def _handle_emoji_summon(
        self,
        instance_name: str,
        channel: str,
        message_ts: str,
        user: str,
        say,
    ) -> None:
        """Summon an instance by reacting with its name as an emoji.

        Fetches the reacted message text, builds a prompt, and executes
        against the named instance in a thread off that message.
        """
        # Fetch the message that was reacted to
        try:
            result = await self._app.client.conversations_history(
                channel=channel,
                latest=message_ts,
                inclusive=True,
                limit=1,
            )
            messages = result.get("messages", [])
            if not messages:
                logger.warning("Emoji summon: could not fetch message %s", message_ts)
                return
            original_text = messages[0].get("text", "")
        except Exception:
            logger.exception("Emoji summon: error fetching message %s", message_ts)
            return

        if not original_text:
            return

        instance = self._config.get_instance(instance_name)
        conversation_id = f"{channel}:{message_ts}"

        # Get channel name for context
        channel_config = await self._get_channel_config(channel)
        prompt = self._build_prompt(original_text, user, channel, channel_config.name)

        logger.info(
            "Emoji summon: %s summoned %s on %s",
            user,
            instance_name,
            message_ts,
        )

        await self._execute_with_progress(
            instance_name,
            instance,
            conversation_id,
            prompt,
            channel,
            message_ts,  # thread_ts = the reacted message
            message_ts,  # user_ts = same (for ⏳ reaction)
            say,
        )

    def _build_roundtable_prompt(self, base_prompt: str, instance_name: str) -> str:
        """Wrap a prompt with roundtable instructions for one instance."""
        others = [n for n in self._config.instance_names if n != instance_name]
        return (
            f"[ROUNDTABLE MODE — Multiple AI instances are in this conversation.\n"
            f"Other instances: {', '.join(others)}\n"
            f"Respond ONLY if you have a unique, valuable perspective.\n"
            f"If you have nothing substantive to add, respond with exactly: [PASS]\n"
            f"Do not repeat or rephrase what another instance would say.]\n\n"
            f"{base_prompt}"
        )

    async def _execute_roundtable(
        self,
        conversation_id: str,
        base_prompt: str,
        channel: str,
        thread_ts: str,
        user_ts: str,
        say,
        *,
        onboarding: object | None = None,
        is_new_thread: bool = False,
    ) -> None:
        """Execute a roundtable: fan out to all instances concurrently.

        Each instance gets the prompt wrapped with roundtable instructions.
        Responses containing [PASS] are filtered out. Remaining responses
        are posted with a stagger delay to stay under Slack rate limits.
        """
        import asyncio

        # React with ⏳ on the user's message
        try:
            await self._app.client.reactions_add(
                channel=channel,
                timestamp=user_ts,
                name="hourglass_flowing_sand",
            )
        except Exception:
            pass

        # Post editable status message
        status_msg = None
        try:
            result = await self._app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="\u2699\ufe0f Roundtable — gathering perspectives...",
            )
            status_msg = result.get("ts")
        except Exception:
            logger.debug("Could not post roundtable status message")

        try:
            # Execute all instances concurrently
            instance_names = self._config.instance_names

            async def _run_one(name: str) -> tuple[str, str]:
                rt_prompt = self._build_roundtable_prompt(base_prompt, name)
                response = await self._service.execute(name, conversation_id, rt_prompt)
                return name, response

            results = await asyncio.gather(
                *[_run_one(name) for name in instance_names],
                return_exceptions=True,
            )

            # Filter out [PASS] responses and errors
            responses: list[tuple[str, str]] = []
            for r in results:
                if isinstance(r, Exception):
                    logger.warning("Roundtable instance error: %s", r)
                    continue
                name, text = r
                if text.strip() == "[PASS]":
                    continue
                responses.append((name, text))

            # Delete status message
            if status_msg:
                try:
                    await self._app.client.chat_delete(channel=channel, ts=status_msg)
                except Exception:
                    pass

            # Post responses with stagger
            first_posted = True
            for i, (name, text) in enumerate(responses):
                instance = self._config.get_instance(name)
                response_text = markdown_to_slack(text)

                # Append onboarding suffix only to the first response
                if (
                    first_posted
                    and onboarding
                    and hasattr(onboarding, "get_response_suffix")
                ):
                    suffix = onboarding.get_response_suffix(is_new_thread, 0.0, False)
                    if suffix:
                        response_text = f"{response_text}\n{suffix}"
                    first_posted = False

                result = await say(
                    text=response_text,
                    thread_ts=thread_ts,
                    username=instance.persona.name,
                    icon_emoji=instance.persona.emoji,
                )
                self._track_prompt(result, name, conversation_id, base_prompt)

                # Stagger between posts (skip delay after last one)
                if i < len(responses) - 1:
                    await asyncio.sleep(1.5)

        except Exception:
            logger.exception("Error in roundtable execution for %s", conversation_id)
            if status_msg:
                try:
                    await self._app.client.chat_delete(channel=channel, ts=status_msg)
                except Exception:
                    pass

        finally:
            # Remove ⏳ reaction
            try:
                await self._app.client.reactions_remove(
                    channel=channel,
                    timestamp=user_ts,
                    name="hourglass_flowing_sand",
                )
            except Exception:
                pass

            # Record thread as roundtable-owned
            self._set_thread_owner(conversation_id, "_ROUNDTABLE")

    async def _handle_approval_action(self, ack, body) -> None:
        """Handle Block Kit button clicks for the approval system."""
        await ack()  # Acknowledge immediately (Slack requires within 3 seconds)

        actions = body.get("actions", [])
        if not actions:
            return

        action = actions[0]
        action_id = action.get("action_id", "")
        value = action.get("value", "")

        logger.info("Approval action: %s → %s", action_id, value)

        # Find the matching approval system across all active sessions
        if hasattr(self._service, "_approval_systems"):
            for session_key, approval in self._service._approval_systems.items():
                if hasattr(approval, "resolve_approval"):
                    if approval.resolve_approval(action_id, value):
                        logger.info("Approval resolved for session %s", session_key)
                        return

        logger.debug("No pending approval matched action %s", action_id)

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
