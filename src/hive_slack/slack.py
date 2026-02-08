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

    Key differences:
    - **bold** → *bold*
    - [text](url) → <url|text>
    - # Heading → *Heading*
    - ## Heading → *Heading*
    - ### Heading → *Heading*
    """
    # Protect code blocks from transformation
    code_blocks: list[str] = []

    def _save_code_block(match: re.Match) -> str:
        code_blocks.append(match.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\s\S]*?```", _save_code_block, text)

    # Protect inline code from transformation
    inline_codes: list[str] = []

    def _save_inline_code(match: re.Match) -> str:
        inline_codes.append(match.group(0))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`[^`]+`", _save_inline_code, text)

    # Bold: **text** → *text* (but not within words)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # Headings: # Heading → *Heading*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00INLINE{i}\x00", code)

    # Restore code blocks
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODEBLOCK{i}\x00", block)

    return text


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
