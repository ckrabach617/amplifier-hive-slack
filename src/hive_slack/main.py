"""Entry point for the Amplifier Hive Slack connector."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from hive_slack.config import HiveSlackConfig
from hive_slack.service import InProcessSessionManager
from hive_slack.slack import SlackConnector

logger = logging.getLogger(__name__)


def _nicegui_available() -> bool:
    """Check if NiceGUI is installed."""
    try:
        import nicegui  # noqa: F401

        return True
    except ImportError:
        return False


async def run(config_path: str) -> None:
    """Load config, start service, connect to Slack, run until interrupted."""
    logging.basicConfig(
        level=getattr(
            logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO
        ),
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

    connector_task: asyncio.Task[None] | None = None
    watchdog_task: asyncio.Task[None] | None = None
    try:
        connector_task = asyncio.create_task(connector.start())
        watchdog_task = asyncio.create_task(connector.run_watchdog())
        await stop_event.wait()
    except Exception:
        logger.exception("Unexpected error")
    finally:
        logger.info("Shutting down...")
        if watchdog_task is not None:
            watchdog_task.cancel()
        if connector_task is not None:
            connector_task.cancel()
        await connector.stop()
        await service.stop()
        logger.info("Shutdown complete")


def run_with_admin(config_path: str) -> None:
    """Run the bot with the admin web UI (NiceGUI owns the event loop)."""
    from nicegui import app as nicegui_app, ui

    port = int(os.environ.get("ADMIN_PORT", "8080"))
    config = None
    service = None
    connector = None

    @nicegui_app.on_startup
    async def startup():
        nonlocal config, service, connector

        logging.basicConfig(
            level=getattr(
                logging,
                os.environ.get("LOG_LEVEL", "INFO").upper(),
                logging.INFO,
            ),
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )

        config = HiveSlackConfig.from_yaml(config_path)

        # Start service
        service = InProcessSessionManager(config)
        await service.start()

        for name, inst in config.instances.items():
            logger.info(
                "Instance '%s' ready (%s, bundle=%s)",
                name,
                inst.persona.name,
                inst.bundle,
            )
        logger.info("Default instance: %s", config.default_instance)

        # Start connector
        connector = SlackConnector(config, service)
        instance_names = ", ".join(
            f"{inst.persona.name} {inst.persona.emoji}"
            for inst in config.instances.values()
        )
        logger.info("Connecting to Slack with instances: %s", instance_names)

        # Initialize admin UI
        from hive_slack.admin import create_admin_app

        create_admin_app(service, connector, config)

        # Start connector and watchdog as background tasks
        asyncio.create_task(connector.start())
        asyncio.create_task(connector.run_watchdog())

    @nicegui_app.on_shutdown
    async def shutdown():
        if connector:
            await connector.stop()
        if service:
            await service.stop()

    logger.info("Starting with admin UI on port %d", port)
    ui.run(
        port=port,
        title="Hive Slack Admin",
        favicon="🐝",
        show=False,  # Don't auto-open browser
        reload=False,  # Don't watch for file changes
    )


def cli() -> None:
    """CLI entry point with subcommands."""
    args = sys.argv[1:]

    if not args or args[0] not in ("service", "slack", "setup"):
        # Default: run the bot (backward compatible)
        config_path = args[0] if args else "config/example.yaml"

        no_admin = "--no-admin" in sys.argv

        if not no_admin and _nicegui_available():
            run_with_admin(config_path)
        else:
            asyncio.run(run(config_path))
        return

    command = args[0]

    if command == "setup":
        from hive_slack.setup import run_setup

        run_setup()
    elif command == "service":
        _handle_service_command(args[1:])
    elif command == "slack":
        _handle_slack_command(args[1:])


def _handle_service_command(args: list[str]) -> None:
    """Handle 'hive-slack service <subcommand>' commands."""
    from hive_slack import service_manager

    if not args:
        args = ["status"]

    subcmd = args[0]

    if subcmd == "install":
        config_path = args[1] if len(args) > 1 else "config/example.yaml"
        env_file = None
        for i, a in enumerate(args):
            if a == "--env" and i + 1 < len(args):
                env_file = args[i + 1]
        info = service_manager.install(config_path, env_file)
        print(f"Installed: {info.message}")
        print(f"Service file: {info.service_file}")
        print()
        print("Next: hive-slack service start")

    elif subcmd == "uninstall":
        info = service_manager.uninstall()
        print(f"Uninstalled: {info.message}")

    elif subcmd == "start":
        info = service_manager.start()
        print(f"Started: {info.message}")

    elif subcmd == "stop":
        info = service_manager.stop()
        print(f"Stopped: {info.message}")

    elif subcmd == "restart":
        info = service_manager.restart()
        print(f"Restarted: {info.message}")

    elif subcmd == "status":
        info = service_manager.status()
        status_icon = {
            service_manager.ServiceStatus.RUNNING: "\U0001f7e2",
            service_manager.ServiceStatus.STOPPED: "\u26aa",
            service_manager.ServiceStatus.FAILED: "\U0001f534",
            service_manager.ServiceStatus.NOT_INSTALLED: "\u26ab",
            service_manager.ServiceStatus.UNKNOWN: "\u2753",
        }.get(info.status, "\u2753")
        print(f"{status_icon} {info.status.value}: {info.message}")
        if info.pid:
            print(f"   PID: {info.pid}")
        if info.service_file:
            print(f"   Service file: {info.service_file}")

    elif subcmd == "logs":
        follow = "-f" in args or "--follow" in args
        service_manager.logs(follow=follow)

    else:
        print(f"Unknown service command: {subcmd}")
        print("Available: install, uninstall, start, stop, restart, status, logs")
        sys.exit(1)


def _handle_slack_command(args: list[str]) -> None:
    """Handle 'hive-slack slack <subcommand>' commands."""
    from hive_slack import slack_manifest

    if not args:
        args = ["status"]

    subcmd = args[0]

    if subcmd == "export":
        manifest = slack_manifest.export_manifest()
        output = args[1] if len(args) > 1 else None
        if output:
            slack_manifest.save_manifest(manifest, output)
            print(f"Manifest exported to {output}")
        else:
            import yaml as _yaml

            print(_yaml.dump(manifest, default_flow_style=False, sort_keys=False))

    elif subcmd == "sync":
        manifest_path = args[1] if len(args) > 1 else "config/slack-manifest.yaml"
        print(f"Syncing manifest from {manifest_path}...")
        slack_manifest.sync_from_file(manifest_path)
        print("Manifest synced successfully.")
        print()
        print("Note: If you changed OAuth scopes, you need to reinstall:")
        url = slack_manifest.get_reinstall_url()
        print(f"  {url}")

    elif subcmd == "validate":
        manifest_path = args[1] if len(args) > 1 else "config/slack-manifest.yaml"
        import yaml as _yaml

        with open(manifest_path) as f:
            manifest = _yaml.safe_load(f)
        ok, errors = slack_manifest.validate_manifest(manifest)
        if ok:
            print("Manifest is valid.")
        else:
            print(f"Manifest validation failed: {errors}")
            sys.exit(1)

    elif subcmd == "reinstall-url":
        url = slack_manifest.get_reinstall_url()
        print(f"Reinstall URL: {url}")

    elif subcmd == "rotate-token":
        new_token, new_refresh = slack_manifest.rotate_token()
        print("Token rotated successfully.")
        print(f"New config token: {new_token[:20]}...")
        print(f"New refresh token: {new_refresh[:20]}...")
        print()
        print("Update your .env file with these new values:")
        print(f"SLACK_CONFIG_TOKEN={new_token}")
        print(f"SLACK_CONFIG_REFRESH_TOKEN={new_refresh}")

    elif subcmd == "status":
        try:
            manifest = slack_manifest.export_manifest()
            scopes = manifest.get("oauth_config", {}).get("scopes", {}).get("bot", [])
            events = (
                manifest.get("settings", {})
                .get("event_subscriptions", {})
                .get("bot_events", [])
            )
            name = manifest.get("display_information", {}).get("name", "unknown")
            socket_mode = manifest.get("settings", {}).get("socket_mode_enabled", False)
            print(f"App: {name}")
            print(f"Socket Mode: {'enabled' if socket_mode else 'disabled'}")
            print(f"Bot scopes ({len(scopes)}): {', '.join(scopes)}")
            print(f"Bot events ({len(events)}): {', '.join(events)}")
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)

    else:
        print(f"Unknown slack command: {subcmd}")
        print("Available: export, sync, validate, status, reinstall-url, rotate-token")
        sys.exit(1)


if __name__ == "__main__":
    cli()
