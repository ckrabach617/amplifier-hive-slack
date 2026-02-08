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
    """Find markdown tables, convert to code blocks, protect them.

    Tables are converted to fixed-width monospace inside code blocks.
    Markdown formatting is stripped from cell content (bold, italic, links)
    since code blocks render literally.
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
                result.append(protect_fn(_render_table_as_code(table_lines)))
                table_lines = []
                in_table = False
            result.append(line)

    if in_table:
        result.append(protect_fn(_render_table_as_code(table_lines)))

    return "\n".join(result)


def _strip_markdown_inline(text: str) -> str:
    """Strip markdown inline formatting from text (for use in code blocks)."""
    # **bold** or __bold__ → bold
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    # *italic* or _italic_ → italic
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    # [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # `code` → code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


def _render_table_as_code(rows: list[str]) -> str:
    """Render table rows as a fixed-width code block with a header separator."""
    parsed: list[list[str]] = []
    for row in rows:
        cells = [_strip_markdown_inline(c.strip()) for c in row.strip().strip("|").split("|")]
        parsed.append(cells)

    if not parsed:
        return ""

    num_cols = max(len(r) for r in parsed)
    col_widths = [0] * num_cols
    for row in parsed:
        for i, cell in enumerate(row):
            if i < num_cols:
                col_widths[i] = max(col_widths[i], len(cell))

    formatted: list[str] = []
    for row_idx, row in enumerate(parsed):
        cells = []
        for i in range(num_cols):
            cell = row[i] if i < len(row) else ""
            cells.append(cell.ljust(col_widths[i]))
        formatted.append("  ".join(cells))
        # Add separator after header row
        if row_idx == 0 and len(parsed) > 1:
            sep = "  ".join("─" * w for w in col_widths)
            formatted.append(sep)

    return "```\n" + "\n".join(formatted) + "\n```"


class SlackConnector:
    """Slack Socket Mode listener + response poster.

    Listens for @mentions via Socket Mode, routes to SessionManager,
    posts responses in threads with persona customization.
    """

    def __init__(self, config: HiveSlackConfig, service: SessionManager) -> None:
        self._config = config
        self._service = service
        self._app = AsyncApp(token=config.slack.bot_token)
        self._handler = AsyncSocketModeHandler(self._app, config.slack.app_token)

        # Register event handlers
        self._app.event("app_mention")(self._handle_mention)

    async def _handle_mention(self, event: dict, say) -> None:
        """Handle @mention events — the core message flow."""
        text = self._strip_mention(event.get("text", ""))
        if not text:
            return

        channel = event.get("channel", "")
        # thread_ts is set when replying in a thread; ts is this message's timestamp
        thread_ts = event.get("thread_ts") or event.get("ts", "")
        user = event.get("user", "unknown")

        conversation_id = f"{channel}:{thread_ts}"

        logger.info(
            "Mention from %s in %s: %s",
            user,
            conversation_id,
            text[:100],
        )

        try:
            response = await self._service.execute(
                self._config.instance.name,
                conversation_id,
                text,
            )

            await say(
                text=markdown_to_slack(response),
                thread_ts=thread_ts,
                username=self._config.instance.persona.name,
                icon_emoji=self._config.instance.persona.emoji,
            )
        except Exception:
            logger.exception("Error handling mention in %s", conversation_id)
            await say(
                text="Sorry, I encountered an error processing your request.",
                thread_ts=thread_ts,
                username=self._config.instance.persona.name,
                icon_emoji=self._config.instance.persona.emoji,
            )

    @staticmethod
    def _strip_mention(text: str) -> str:
        """Remove <@U12345> mention prefix from message text."""
        return re.sub(r"^<@[A-Z0-9]+>\s*", "", text).strip()

    async def start(self) -> None:
        """Start the Socket Mode handler (blocks until stopped)."""
        logger.info("Starting Slack Socket Mode connection...")
        await self._handler.start_async()

    async def stop(self) -> None:
        """Stop the Socket Mode handler."""
        logger.info("Stopping Slack connector...")
        await self._handler.close_async()
