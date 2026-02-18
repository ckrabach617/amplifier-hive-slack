"""Slack Socket Mode connection lifecycle management.

Handles Socket Mode handler creation, start/stop, reconnection after
OS suspend/resume (WSL2 sleep), and periodic health checks.
"""

from __future__ import annotations

import asyncio
import logging
import time

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from hive_slack.config import HiveSlackConfig

logger = logging.getLogger(__name__)


class SlackConnection:
    """Manages the Slack Socket Mode connection lifecycle.

    Owns the AsyncSocketModeHandler and provides start/stop/reconnect
    with a watchdog for detecting stale connections after OS suspend.
    """

    def __init__(self, app: AsyncApp, config: HiveSlackConfig) -> None:
        self._app = app
        self._config = config
        self._handler = AsyncSocketModeHandler(app, config.slack.app_token)
        self.bot_user_id: str = ""
        self.bot_id: str = ""

        # Tracking fields for /status health reporting
        self._started_at: float | None = None
        self._last_health_check_at: float | None = None
        self._reconnect_count: int = 0

    @property
    def started_at(self) -> float | None:
        """Monotonic timestamp when start() was called."""
        return self._started_at

    @property
    def last_health_check_at(self) -> float | None:
        """Monotonic timestamp of last successful health check."""
        return self._last_health_check_at

    @property
    def reconnect_count(self) -> int:
        """Number of reconnections since start."""
        return self._reconnect_count

    async def start(self) -> None:
        """Start the Socket Mode handler (blocks until stopped)."""
        self._started_at = time.monotonic()
        logger.info("Starting Slack Socket Mode connection...")

        # Get our own bot user ID for filtering @mentions in message handlers
        try:
            auth = await self._app.client.auth_test()
            self.bot_user_id = auth.get("user_id", "")
            logger.info("Bot user ID: %s", self.bot_user_id)
        except Exception:
            logger.warning("Could not determine bot user ID")
            self.bot_user_id = ""

        await self._handler.start_async()

    async def stop(self) -> None:
        """Stop the Socket Mode handler."""
        logger.info("Stopping Slack connection...")
        await self._handler.close_async()

    async def reconnect(self) -> None:
        """Force a fresh Socket Mode connection.

        Closes the current handler and starts a new one. Used by the
        connection watchdog to recover from stale websockets after
        OS suspend/resume (e.g. WSL2 sleep).
        """
        self._reconnect_count += 1
        logger.info("Forcing Socket Mode reconnection...")
        try:
            await self._handler.close_async()
        except Exception:
            logger.warning("Error closing old handler", exc_info=True)

        # Create a fresh handler (reuses the same app and its event registrations)
        self._handler = AsyncSocketModeHandler(self._app, self._config.slack.app_token)
        await self._handler.connect_async()
        logger.info("Reconnected to Slack successfully")

    async def run_watchdog(self, interval: float = 15.0) -> None:
        """Detect OS suspend/resume via wall-clock time jumps and reconnect.

        Runs in a loop, sleeping for ``interval`` seconds. If wall-clock time
        advanced by more than 2x the interval, we likely resumed from suspend
        and the websocket is stale -- trigger a reconnect.

        Also periodically verifies the connection is alive via auth.test.
        """
        last_check = time.monotonic()
        last_wall = time.time()
        health_check_counter = 0

        while True:
            await asyncio.sleep(interval)
            now_mono = time.monotonic()
            now_wall = time.time()
            elapsed_mono = now_mono - last_check
            elapsed_wall = now_wall - last_wall

            # Detect time jump: wall clock advanced much more than monotonic
            # sleep should allow. This happens when the OS was suspended.
            if elapsed_wall > elapsed_mono + interval:
                jump = elapsed_wall - elapsed_mono
                logger.warning(
                    "Wall-clock jumped %.1fs beyond expected -- "
                    "OS likely suspended. Forcing reconnect.",
                    jump,
                )
                try:
                    await self.reconnect()
                except Exception:
                    logger.exception("Reconnect failed after time jump")

            # Also do a periodic health check every ~2 minutes (8 intervals)
            health_check_counter += 1
            if health_check_counter >= 8:
                health_check_counter = 0
                try:
                    await asyncio.wait_for(self._app.client.auth_test(), timeout=10.0)
                    self._last_health_check_at = time.monotonic()
                except Exception:
                    logger.warning(
                        "Health check (auth.test) failed -- forcing reconnect",
                        exc_info=True,
                    )
                    try:
                        await self.reconnect()
                    except Exception:
                        logger.exception("Reconnect failed after health check failure")

            last_check = now_mono
            last_wall = now_wall
