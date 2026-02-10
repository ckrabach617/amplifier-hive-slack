"""Shared layout components for the admin UI."""

from __future__ import annotations

import time

from nicegui import ui

import hive_slack.admin as admin_state


def admin_layout(title: str = "Dashboard") -> None:
    """Render the shared page header and navigation."""
    ui.page_title(f"Hive Admin — {title}")

    with ui.header().classes("bg-blue-900 text-white items-center"):
        ui.label("🐝 Hive Slack Admin").classes("text-lg font-bold")
        with ui.row().classes("gap-4 ml-8"):
            ui.link("Dashboard", "/admin").classes("text-white no-underline")
            ui.link("Slack", "/admin/slack").classes("text-white no-underline")
            ui.link("Config", "/admin/config").classes("text-white no-underline")
            ui.link("Logs", "/admin/logs").classes("text-white no-underline")


def format_uptime(start_time: float) -> str:
    """Format uptime from a start timestamp."""
    elapsed = int(time.time() - start_time)
    if elapsed < 60:
        return f"{elapsed}s"
    if elapsed < 3600:
        m, s = divmod(elapsed, 60)
        return f"{m}m {s}s"
    h, remainder = divmod(elapsed, 3600)
    m, _ = divmod(remainder, 60)
    return f"{h}h {m}m"


def status_badge(is_ok: bool, ok_text: str = "OK", fail_text: str = "Down") -> ui.badge:
    """Create a colored status badge."""
    if is_ok:
        return ui.badge(ok_text, color="green")
    else:
        return ui.badge(fail_text, color="red")
