"""Entry point for the Amplifier Hive Slack connector."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from hive_slack.config import HiveSlackConfig
from hive_slack.service import InProcessSessionManager
from hive_slack.slack import SlackConnector

logger = logging.getLogger(__name__)


async def run(config_path: str) -> None:
    """Load config, start service, connect to Slack, run until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("Loading config from %s", config_path)
    config = HiveSlackConfig.from_yaml(config_path)

    # Start the service (loads Amplifier bundle — may take 30-60 seconds)
    service = InProcessSessionManager(config)
    await service.start()
    logger.info("Instance '%s' ready", config.instance.name)

    # Create the Slack connector
    connector = SlackConnector(config, service)

    # Graceful shutdown on Ctrl+C / SIGTERM
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def handle_signal() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    logger.info(
        "Connecting to Slack as '%s' %s",
        config.instance.persona.name,
        config.instance.persona.emoji,
    )

    try:
        # Start Socket Mode in a background task
        asyncio.create_task(connector.start())
        # Wait for shutdown signal
        await stop_event.wait()
    except Exception:
        logger.exception("Unexpected error")
    finally:
        logger.info("Shutting down...")
        await connector.stop()
        await service.stop()
        logger.info("Shutdown complete")


def cli() -> None:
    """CLI entry point."""
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/hive.yaml"
    asyncio.run(run(config_path))


if __name__ == "__main__":
    cli()
