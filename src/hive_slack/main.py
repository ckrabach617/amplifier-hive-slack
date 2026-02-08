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

    # Start the service (loads Amplifier bundles)
    service = InProcessSessionManager(config)
    await service.start()

    for name, inst in config.instances.items():
        logger.info(
            "Instance '%s' ready (%s %s, bundle=%s)",
            name,
            inst.persona.name,
            inst.persona.emoji,
            inst.bundle,
        )
    logger.info("Default instance: %s", config.default_instance)

    # Create the Slack connector
    connector = SlackConnector(config, service)

    # Graceful shutdown
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def handle_signal() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    instance_names = ", ".join(
        f"{inst.persona.name} {inst.persona.emoji}"
        for inst in config.instances.values()
    )
    logger.info("Connecting to Slack with instances: %s", instance_names)

    try:
        asyncio.create_task(connector.start())
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
