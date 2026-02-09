"""Connector-provided Slack tools for Amplifier sessions.

These tools are mounted on each session post-creation, giving the
Amplifier instance the ability to act in Slack -- send messages,
add reactions, etc.

The tools capture the connector's authenticated Slack WebClient
via constructor injection. No new module system needed.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SlackSendMessageTool:
    """Send a message in Slack."""

    def __init__(
        self, slack_client, default_channel: str, default_thread_ts: str = ""
    ) -> None:
        self._client = slack_client
        self._default_channel = default_channel
        self._default_thread_ts = default_thread_ts

    @property
    def name(self) -> str:
        return "slack_send_message"

    @property
    def description(self) -> str:
        return (
            "Send a message in Slack. Posts to the current conversation thread by default. "
            "Can also post to a different channel. Use for notifications, summaries, or updates."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The message text (markdown supported)",
                },
                "channel": {
                    "type": "string",
                    "description": "Channel name or ID to post to (optional — defaults to current channel)",
                },
                "thread_ts": {
                    "type": "string",
                    "description": "Thread timestamp to reply in (optional — defaults to current thread)",
                },
            },
            "required": ["text"],
        }

    async def execute(self, input: dict[str, Any]) -> Any:
        """Send a message to Slack."""
        from amplifier_core.models import ToolResult

        text = input.get("text", "")
        channel = input.get("channel", self._default_channel)
        thread_ts = input.get("thread_ts", self._default_thread_ts)

        if not text:
            return ToolResult(success=False, output="No text provided")

        try:
            kwargs: dict[str, Any] = {"channel": channel, "text": text}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts

            await self._client.chat_postMessage(**kwargs)
            return ToolResult(
                success=True,
                output=f"Message sent to {channel}"
                + (f" in thread {thread_ts}" if thread_ts else ""),
            )
        except Exception as e:
            return ToolResult(success=False, output=f"Failed to send message: {e}")


class SlackReactionTool:
    """Add an emoji reaction to a message in Slack."""

    def __init__(
        self, slack_client, default_channel: str, last_user_ts: str = ""
    ) -> None:
        self._client = slack_client
        self._default_channel = default_channel
        self._last_user_ts = last_user_ts

    @property
    def name(self) -> str:
        return "slack_add_reaction"

    @property
    def description(self) -> str:
        return (
            "Add an emoji reaction to a message in Slack. "
            "Use to acknowledge messages, signal status, or mark completion. "
            "Common emoji: thumbsup, white_check_mark, eyes, warning, fire, rocket"
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "emoji": {
                    "type": "string",
                    "description": "Emoji name without colons (e.g., 'thumbsup', 'white_check_mark', 'eyes')",
                },
                "message_ts": {
                    "type": "string",
                    "description": "Timestamp of the message to react to (optional — defaults to the user's last message)",
                },
            },
            "required": ["emoji"],
        }

    async def execute(self, input: dict[str, Any]) -> Any:
        """Add a reaction to a message."""
        from amplifier_core.models import ToolResult

        emoji = input.get("emoji", "")
        message_ts = input.get("message_ts", self._last_user_ts)

        if not emoji:
            return ToolResult(success=False, output="No emoji provided")
        if not message_ts:
            return ToolResult(
                success=False, output="No message timestamp available to react to"
            )

        try:
            await self._client.reactions_add(
                channel=self._default_channel,
                name=emoji,
                timestamp=message_ts,
            )
            return ToolResult(success=True, output=f"Reacted with :{emoji}:")
        except Exception as e:
            return ToolResult(success=False, output=f"Failed to add reaction: {e}")


def create_slack_tools(
    slack_client,
    channel: str,
    thread_ts: str = "",
    user_ts: str = "",
) -> list:
    """Create all connector-provided Slack tools.

    Args:
        slack_client: Authenticated Slack WebClient
        channel: Default channel for tool operations
        thread_ts: Default thread timestamp
        user_ts: Timestamp of the user's message (for reactions)

    Returns:
        List of tool instances to mount on a session
    """
    return [
        SlackSendMessageTool(slack_client, channel, thread_ts),
        SlackReactionTool(slack_client, channel, user_ts),
    ]
