"""Slack setup page -- connection status, manifest URL, token input."""

from __future__ import annotations

import os

from nicegui import ui

import hive_slack.admin as admin_state
from hive_slack.admin.shared import admin_layout
from hive_slack.admin.auth import require_auth
from hive_slack.setup import SLACK_MANIFEST, _generate_manifest_url


@ui.page("/admin/slack")
def slack_setup_page() -> None:
    """Render the Slack setup page."""
    if not require_auth():
        return
    admin_layout("Slack")

    connector = admin_state._connector
    config = admin_state._config  # noqa: F841

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        is_connected = bool(getattr(connector, "_bot_user_id", ""))

        if is_connected:
            _render_connected(connector)
        else:
            _render_setup()


def _render_connected(connector) -> None:
    """Show connection status when already connected."""
    ui.label("Slack Connection").classes("text-xl font-bold")

    with ui.card().classes("w-full"):
        bot_user_id = getattr(connector, "_bot_user_id", "unknown")
        ui.label(f"Connected as bot user {bot_user_id}").classes(
            "text-green-600"
        )

        ui.separator()

        # Scopes
        scopes = SLACK_MANIFEST["oauth_config"]["scopes"]["bot"]
        ui.label(f"Bot Scopes ({len(scopes)}):").classes("font-bold mt-2")
        with ui.row().classes("flex-wrap gap-1"):
            for scope in scopes:
                ui.badge(scope).props("color=blue outline")

        ui.separator()

        # Events
        events = SLACK_MANIFEST["settings"]["event_subscriptions"][
            "bot_events"
        ]
        ui.label(f"Event Subscriptions ({len(events)}):").classes(
            "font-bold mt-2"
        )
        with ui.row().classes("flex-wrap gap-1"):
            for event in events:
                ui.badge(event).props("color=green outline")

    # Actions
    with ui.row().classes("mt-4 gap-2"):
        result_label = ui.label("")

        async def test_connection():
            try:
                auth = await connector._app.client.auth_test()
                team = auth.get("team", "unknown")
                user = auth.get("user", "unknown")
                result_label.text = f"Connected to '{team}' as @{user}"
                result_label.classes(
                    "text-green-600",
                    replace="text-green-600 text-red-600",
                )
            except Exception as e:
                result_label.text = f"Error: {e}"
                result_label.classes(
                    "text-red-600",
                    replace="text-green-600 text-red-600",
                )

        ui.button("Test Connection", on_click=test_connection).props(
            "color=primary"
        )

        app_id = os.environ.get("SLACK_APP_ID", "")
        if app_id:
            reinstall_url = (
                f"https://api.slack.com/apps/{app_id}/install-on-team"
            )
            ui.link("Reinstall App", reinstall_url, new_tab=True).classes(
                "bg-gray-200 px-4 py-2 rounded text-gray-700 no-underline"
            )


def _render_setup() -> None:
    """Show setup wizard when not yet connected."""
    ui.label("Set Up Slack Connection").classes("text-xl font-bold")

    # Step 1: Create app
    with ui.card().classes("w-full"):
        ui.label("Step 1: Create your Slack app").classes("font-bold")
        ui.label(
            "Click the button below to create a pre-configured Slack app:"
        )
        manifest_url = _generate_manifest_url()
        ui.link("Create Slack App", manifest_url, new_tab=True).classes(
            "bg-blue-600 text-white px-4 py-2 rounded no-underline "
            "inline-block mt-2"
        )
        ui.label(
            "This opens Slack with all permissions already configured. "
            "Just select your workspace and click Create."
        ).classes("text-sm text-gray-500 mt-2")

    # Step 2: Tokens
    with ui.card().classes("w-full"):
        ui.label("Step 2: Enter your tokens").classes("font-bold")
        ui.label(
            "After creating the app, copy these tokens from the "
            "Slack app settings:"
        ).classes("text-sm text-gray-500")

        bot_input = ui.input("Bot Token (xoxb-...)").classes("w-full")
        app_input = ui.input("App Token (xapp-...)").classes("w-full")
        ui.label(
            "App Token: go to Basic Information -> App-Level Tokens -> "
            "Generate Token -> name it anything, add scope connections:write"
        ).classes("text-xs text-gray-400")

        result_label = ui.label("")

        async def test_tokens():
            from slack_sdk.web.async_client import AsyncWebClient

            try:
                client = AsyncWebClient(token=bot_input.value)
                auth = await client.auth_test()
                team = auth.get("team", "unknown")
                user = auth.get("user", "unknown")
                result_label.text = f"Connected to '{team}' as @{user}"
                result_label.classes(
                    "text-green-600",
                    replace="text-green-600 text-red-600",
                )
            except Exception as e:
                result_label.text = f"Error: {e}"
                result_label.classes(
                    "text-red-600",
                    replace="text-green-600 text-red-600",
                )

        def save_tokens():
            from pathlib import Path

            env_path = Path(".env")
            lines = []
            if env_path.exists():
                lines = env_path.read_text().splitlines()

            # Update or append tokens
            updated = {}
            if bot_input.value:
                updated["SLACK_BOT_TOKEN"] = bot_input.value
            if app_input.value:
                updated["SLACK_APP_TOKEN"] = app_input.value

            new_lines = []
            for line in lines:
                key = line.split("=", 1)[0] if "=" in line else ""
                if key in updated:
                    new_lines.append(f"{key}={updated.pop(key)}")
                else:
                    new_lines.append(line)
            for key, val in updated.items():
                new_lines.append(f"{key}={val}")

            env_path.write_text("\n".join(new_lines) + "\n")
            result_label.text = (
                "Tokens saved to .env. Restart the service to apply."
            )
            result_label.classes(
                "text-green-600",
                replace="text-green-600 text-red-600",
            )

        with ui.row().classes("gap-2 mt-2"):
            ui.button("Test Connection", on_click=test_tokens).props(
                "color=primary"
            )
            ui.button("Save to .env", on_click=save_tokens).props(
                "color=secondary"
            )
