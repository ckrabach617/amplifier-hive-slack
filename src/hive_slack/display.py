"""Slack implementation of Amplifier's DisplaySystem protocol.

Routes hook display messages (info, warning, error) to the Slack channel
where the conversation is happening.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

logger = logging.getLogger(__name__)


class SlackDisplaySystem:
    """Post hook messages to a Slack channel/thread.

    Satisfies the DisplaySystem protocol from amplifier-core.
    """

    def __init__(self, slack_client, channel: str, thread_ts: str = "") -> None:
        self._client = slack_client
        self._channel = channel
        self._thread_ts = thread_ts
        self._background_tasks: set[asyncio.Task] = set()

    def show_message(
        self,
        message: str,
        level: Literal["info", "warning", "error"] = "info",
        source: str = "hook",
    ) -> None:
        """Post a message to the Slack channel. Fire-and-forget."""
        prefix = {"warning": "\u26a0\ufe0f ", "error": "\ud83d\udea8 "}.get(level, "")
        text = f"{prefix}{message}"

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._post(text))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except RuntimeError:
            # No running event loop — just log
            logger.info("[display:%s] %s", level, message)

    async def _post(self, text: str) -> None:
        """Post to Slack. Best-effort — never raises."""
        try:
            await self._client.chat_postMessage(
                channel=self._channel,
                thread_ts=self._thread_ts,
                text=text,
            )
        except Exception:
            logger.debug("Failed to post display message to Slack", exc_info=True)
