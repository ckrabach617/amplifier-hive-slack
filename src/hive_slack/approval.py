"""Slack implementation of Amplifier's ApprovalSystem protocol.

Posts Block Kit interactive buttons in Slack and waits for the user
to click one. Used for tool confirmations, destructive operation guards, etc.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Literal

logger = logging.getLogger(__name__)


class SlackApprovalSystem:
    """Interactive approval via Slack Block Kit buttons.

    Satisfies the ApprovalSystem protocol from amplifier-core.

    When request_approval() is called:
    1. Posts a message with Block Kit buttons (one per option)
    2. Waits for the user to click a button (via block_actions event)
    3. Returns the selected option string

    The SlackConnector must register a block_actions handler that calls
    resolve_approval() when a button is clicked.
    """

    def __init__(self, slack_client, channel: str, thread_ts: str = "") -> None:
        self._client = slack_client
        self._channel = channel
        self._thread_ts = thread_ts
        # Pending approvals: correlation_id -> (event, result)
        self._pending: dict[str, tuple[asyncio.Event, list[str]]] = {}

    async def request_approval(
        self,
        prompt: str,
        options: list[str],
        timeout: float,
        default: Literal["allow", "deny"],
    ) -> str:
        """Post approval buttons and wait for user response."""
        correlation_id = str(uuid.uuid4())[:8]

        # Build Block Kit blocks
        buttons = []
        for option in options:
            button: dict = {
                "type": "button",
                "text": {"type": "plain_text", "text": option},
                "action_id": f"approval_{correlation_id}_{option}",
                "value": option,
            }
            # Add style for well-known options
            lower = option.lower()
            if lower in ("allow", "yes", "approve"):
                button["style"] = "primary"
            elif lower in ("deny", "no", "reject"):
                button["style"] = "danger"
            buttons.append(button)

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": prompt},
            },
            {
                "type": "actions",
                "elements": buttons,
            },
        ]

        # Post the approval message
        event = asyncio.Event()
        result_holder: list[str] = []
        self._pending[correlation_id] = (event, result_holder)

        try:
            msg = await self._client.chat_postMessage(
                channel=self._channel,
                thread_ts=self._thread_ts,
                text=prompt,  # Fallback for notifications
                blocks=blocks,
            )
            msg_ts = msg.get("ts", "")

            # Wait for user response with timeout
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
                selected = result_holder[0] if result_holder else default
            except asyncio.TimeoutError:
                logger.info(
                    "Approval timed out after %.0fs, using default: %s",
                    timeout,
                    default,
                )
                selected = default

            # Update the message to show the result (remove buttons)
            try:
                result_text = f"{prompt}\n\n*Selected: {selected}*"
                await self._client.chat_update(
                    channel=self._channel,
                    ts=msg_ts,
                    text=result_text,
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": result_text},
                        }
                    ],
                )
            except Exception:
                logger.debug("Failed to update approval message", exc_info=True)

            return selected

        finally:
            self._pending.pop(correlation_id, None)

    def resolve_approval(self, action_id: str, value: str) -> bool:
        """Called by the connector when a block_actions event arrives.

        Returns True if this action was for a pending approval, False otherwise.
        """
        # Parse correlation_id from action_id: "approval_{correlation_id}_{option}"
        parts = action_id.split("_", 2)
        if len(parts) < 2:
            return False

        correlation_id = parts[1]
        if correlation_id not in self._pending:
            return False

        event, result_holder = self._pending[correlation_id]
        result_holder.append(value)
        event.set()
        logger.info("Approval resolved: %s -> %s", correlation_id, value)
        return True
