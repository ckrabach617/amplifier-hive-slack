"""Live log viewer page -- tail logs with filtering."""

from __future__ import annotations

import logging
import time
from collections import deque

from nicegui import ui

import hive_slack.admin as admin_state  # noqa: F401
from hive_slack.admin.shared import admin_layout
from hive_slack.admin.auth import require_auth

# Ring buffer for log records
_log_buffer: deque[dict] = deque(maxlen=2000)


class _RingBufferHandler(logging.Handler):
    """Capture all log records into a ring buffer for the log viewer."""

    def emit(self, record: logging.LogRecord) -> None:
        _log_buffer.append(
            {
                "time": time.strftime(
                    "%H:%M:%S", time.localtime(record.created)
                ),
                "level": record.levelname,
                "name": record.name,
                "message": record.getMessage(),
            }
        )


# Install on root logger
_ring_handler = _RingBufferHandler()
_ring_handler.setLevel(logging.DEBUG)
logging.getLogger().addHandler(_ring_handler)


@ui.page("/admin/logs")
def logs_page() -> None:
    """Render the live log viewer page."""
    if not require_auth():
        return
    admin_layout("Logs")

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        ui.label("Live Logs").classes("text-xl font-bold")

        # Filters
        with ui.row().classes("gap-4 items-center"):
            level_filter = ui.select(
                ["ALL", "DEBUG", "INFO", "WARNING", "ERROR"],
                value="INFO",
                label="Min Level",
            ).classes("w-32")
            source_filter = ui.input(
                "Filter by source", placeholder="e.g. hive_slack"
            ).classes("w-64")
            ui.label(
                f"Buffer: {len(_log_buffer)} / 2000 records"
            ).classes("text-sm text-gray-400").bind_text_from(
                level_filter,
                "value",
                backward=lambda _: (
                    f"Buffer: {len(_log_buffer)} / 2000 records"
                ),
            )

        # Log display
        log_container = ui.column().classes("w-full font-mono text-sm")

        _level_order = {
            "DEBUG": 0,
            "INFO": 1,
            "WARNING": 2,
            "ERROR": 3,
            "CRITICAL": 4,
        }

        def refresh_logs() -> None:
            """Refresh the log display."""
            min_level = level_filter.value
            source = source_filter.value.strip().lower()
            min_order = (
                _level_order.get(min_level, 0) if min_level != "ALL" else -1
            )

            log_container.clear()
            with log_container:
                shown = 0
                for record in reversed(list(_log_buffer)):
                    if shown >= 100:
                        break
                    record_order = _level_order.get(record["level"], 0)
                    if record_order < min_order:
                        continue
                    if source and source not in record["name"].lower():
                        continue

                    level = record["level"]
                    color = {
                        "DEBUG": "text-gray-400",
                        "INFO": "text-gray-700",
                        "WARNING": "text-orange-600",
                        "ERROR": "text-red-600",
                        "CRITICAL": "text-red-800 font-bold",
                    }.get(level, "text-gray-700")

                    line = (
                        f"{record['time']}  {level:8s}  "
                        f"{record['name']:30s}  {record['message']}"
                    )
                    ui.label(line).classes(
                        f"{color} whitespace-pre-wrap break-all"
                    )
                    shown += 1

                if shown == 0:
                    ui.label("No matching log entries.").classes(
                        "text-gray-400"
                    )

        ui.timer(2.0, refresh_logs)
        refresh_logs()
