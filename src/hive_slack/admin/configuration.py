"""Configuration viewer page -- read-only view of running config."""

from __future__ import annotations

import os
from pathlib import Path

from nicegui import ui

import hive_slack.admin as admin_state
from hive_slack.admin.shared import admin_layout


def _mask_key(key: str) -> str:
    """Mask an API key for display: show first 8 and last 4 chars."""
    if not key or len(key) < 16:
        return "****"
    return f"{key[:8]}...{key[-4:]}"


def _detect_provider_info() -> tuple[str, str]:
    """Detect the configured AI provider and its masked key."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "Anthropic (Claude)", _mask_key(os.environ["ANTHROPIC_API_KEY"])
    if os.environ.get("OPENAI_API_KEY"):
        return "OpenAI (GPT)", _mask_key(os.environ["OPENAI_API_KEY"])
    if os.environ.get("GOOGLE_API_KEY"):
        return "Google (Gemini)", _mask_key(os.environ["GOOGLE_API_KEY"])
    if os.environ.get("GEMINI_API_KEY"):
        return "Google (Gemini)", _mask_key(os.environ["GEMINI_API_KEY"])
    return "None detected", ""


def _format_size(size: int) -> str:
    """Format bytes to human-readable."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@ui.page("/admin/config")
def config_page() -> None:
    """Render the configuration viewer page."""
    admin_layout("Configuration")

    config = admin_state._config

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        ui.label("Configuration").classes("text-xl font-bold")
        ui.label("Read-only view of the running configuration.").classes(
            "text-gray-500"
        )

        if config is None:
            ui.label("No configuration loaded.").classes("text-red-600")
            return

        # Instances
        ui.label("Instances").classes("text-lg font-bold mt-4")
        for name, inst in config.instances.items():
            with ui.card().classes("w-full"):
                ui.label(
                    f"{inst.persona.emoji} {inst.persona.name}"
                ).classes("text-lg font-bold")
                with ui.grid(columns=2).classes("gap-x-4 gap-y-1"):
                    ui.label("Name:").classes("text-gray-500")
                    ui.label(name)
                    ui.label("Bundle:").classes("text-gray-500")
                    ui.label(inst.bundle)
                    ui.label("Working Dir:").classes("text-gray-500")
                    ui.label(inst.working_dir)

                # File listing for working dir
                working_path = Path(inst.working_dir).expanduser()
                if working_path.exists():
                    files = sorted(working_path.iterdir())
                    dirs = [f for f in files if f.is_dir()]
                    regular = [f for f in files if f.is_file()]
                    ui.label(
                        f"Files: {len(regular)} files, "
                        f"{len(dirs)} directories"
                    ).classes("text-sm text-gray-400 mt-2")

                    with ui.expansion("Browse files", icon="folder").classes(
                        "w-full"
                    ):
                        for d in dirs[:20]:
                            ui.label(f"📁 {d.name}/").classes(
                                "text-sm font-mono"
                            )
                        for f in regular[:30]:
                            try:
                                size = _format_size(f.stat().st_size)
                            except Exception:
                                size = "?"
                            ui.label(f"📄 {f.name}  ({size})").classes(
                                "text-sm font-mono"
                            )
                        if len(files) > 50:
                            ui.label(
                                f"... and {len(files) - 50} more"
                            ).classes("text-sm text-gray-400")
                else:
                    ui.label(
                        "Working directory does not exist"
                    ).classes("text-sm text-orange-600")

        # AI Provider
        ui.label("AI Provider").classes("text-lg font-bold mt-4")
        provider_name, masked_key = _detect_provider_info()
        with ui.card().classes("w-full"):
            with ui.grid(columns=2).classes("gap-x-4 gap-y-1"):
                ui.label("Provider:").classes("text-gray-500")
                ui.label(provider_name)
                if masked_key:
                    ui.label("API Key:").classes("text-gray-500")
                    ui.label(masked_key).classes("font-mono")
                    ui.label("Status:").classes("text-gray-500")
                    ui.label("Key detected from environment").classes(
                        "text-green-600"
                    )

        # Service controls
        ui.label("Service").classes("text-lg font-bold mt-4")
        with ui.card().classes("w-full"):
            restart_result = ui.label("")

            async def restart_service():
                import subprocess

                try:
                    result = subprocess.run(
                        ["systemctl", "--user", "restart", "hive-slack"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode == 0:
                        restart_result.text = (
                            "Service restart initiated. "
                            "Page will reconnect shortly."
                        )
                        restart_result.classes("text-green-600")
                    else:
                        restart_result.text = (
                            f"Restart failed: {result.stderr}"
                        )
                        restart_result.classes("text-red-600")
                except Exception as e:
                    restart_result.text = f"Error: {e}"
                    restart_result.classes("text-red-600")

            ui.button("Restart Service", on_click=restart_service).props(
                "color=orange"
            )
            ui.label(
                "Restarts the systemd service. "
                "The page will briefly disconnect."
            ).classes("text-xs text-gray-400")
