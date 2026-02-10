"""Dashboard page -- overview of bot status, instances, and recent errors."""

from __future__ import annotations

import logging
import time

from nicegui import ui

import hive_slack.admin as admin_state
from hive_slack.admin.shared import admin_layout, format_uptime


# Ring buffer for recent log errors
_recent_errors: list[str] = []
_MAX_ERRORS = 50


class _ErrorCapture(logging.Handler):
    """Capture WARNING+ log records for the dashboard error display."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            ts = time.strftime("%H:%M", time.localtime(record.created))
            msg = f"{ts}  {record.getMessage()}"
            _recent_errors.append(msg)
            if len(_recent_errors) > _MAX_ERRORS:
                _recent_errors.pop(0)


# Install the error capture handler on the root logger
_error_handler = _ErrorCapture()
_error_handler.setLevel(logging.WARNING)
logging.getLogger().addHandler(_error_handler)


@ui.page("/admin")
def dashboard_page() -> None:
    """Render the dashboard page."""
    admin_layout("Dashboard")

    service = admin_state._service
    connector = admin_state._connector
    config = admin_state._config

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        # Status cards row
        with ui.row().classes("gap-4 w-full"):
            # Bot status card
            with ui.card().classes("flex-1"):
                bot_icon = ui.icon("circle", color="grey").classes("text-2xl")
                ui.label("Bot").classes("text-lg font-bold")
                bot_status = ui.label("Checking...")
                bot_uptime = ui.label("")

            # Slack connection card
            with ui.card().classes("flex-1"):
                slack_icon = ui.icon("circle", color="grey").classes("text-2xl")
                ui.label("Slack").classes("text-lg font-bold")
                slack_status = ui.label("Checking...")
                slack_workspace = ui.label("")  # noqa: F841

            # Sessions card
            with ui.card().classes("flex-1"):
                session_count = ui.label("\u2014").classes(
                    "text-3xl font-bold text-blue-600"
                )
                ui.label("Active Sessions")

        # Instances table
        ui.label("Instances").classes("text-lg font-bold")
        instances_table = ui.table(
            columns=[
                {
                    "name": "persona",
                    "label": "Instance",
                    "field": "persona",
                    "align": "left",
                },
                {
                    "name": "bundle",
                    "label": "Bundle",
                    "field": "bundle",
                    "align": "left",
                },
                {
                    "name": "sessions",
                    "label": "Sessions",
                    "field": "sessions",
                    "align": "center",
                },
                {
                    "name": "working_dir",
                    "label": "Working Dir",
                    "field": "working_dir",
                    "align": "left",
                },
            ],
            rows=[],
        ).classes("w-full")

        # Recent errors
        ui.label("Recent Errors").classes("text-lg font-bold")
        error_container = ui.column().classes("w-full")

    def refresh() -> None:
        """Refresh all dashboard data."""
        if service is None or config is None:
            return

        # Bot status
        is_running = bool(getattr(service, "_prepared", None))
        bot_icon.props(f'color={"green" if is_running else "red"}')
        bot_status.text = "Running" if is_running else "Stopped"
        bot_uptime.text = (
            format_uptime(admin_state._start_time) if is_running else ""
        )

        # Slack status
        is_connected = bool(getattr(connector, "_bot_user_id", ""))
        slack_icon.props(f'color={"green" if is_connected else "red"}')
        slack_status.text = "Connected" if is_connected else "Disconnected"

        # Session count
        sessions = getattr(service, "_sessions", {})
        session_count.text = str(len(sessions))

        # Instances table
        rows = []
        for name, inst in config.instances.items():
            count = sum(1 for k in sessions if k.startswith(f"{name}:"))
            rows.append(
                {
                    "persona": f"{inst.persona.emoji} {inst.persona.name}",
                    "bundle": inst.bundle,
                    "sessions": str(count),
                    "working_dir": inst.working_dir,
                }
            )
        instances_table.rows = rows

        # Recent errors
        error_container.clear()
        with error_container:
            if _recent_errors:
                for err in _recent_errors[-10:]:
                    ui.label(err).classes("text-sm text-red-600 font-mono")
            else:
                ui.label("No errors recorded.").classes(
                    "text-sm text-gray-400"
                )

    ui.timer(5.0, refresh)
    refresh()
