"""Interactive setup wizard for Amplifier Hive Slack.

Guides the user through:
1. Creating a Slack app (via manifest URL)
2. Collecting tokens
3. Configuring AI provider
4. Setting up the working directory
5. Writing .env and config files
6. Optionally installing as a systemd service

Usage:
    hive-slack setup
    python -m hive_slack.setup
"""

from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

# The complete Slack app manifest — pre-configures everything
SLACK_MANIFEST = {
    "display_information": {
        "name": "Amplifier",
    },
    "features": {
        "bot_user": {
            "display_name": "Amplifier",
            "always_online": True,
        },
    },
    "oauth_config": {
        "scopes": {
            "bot": [
                "app_mentions:read",
                "channels:history",
                "channels:read",
                "chat:write",
                "chat:write.customize",
                "files:read",
                "files:write",
                "groups:history",
                "groups:read",
                "im:history",
                "im:read",
                "reactions:read",
                "reactions:write",
            ]
        }
    },
    "settings": {
        "event_subscriptions": {
            "bot_events": [
                "app_mention",
                "message.channels",
                "message.groups",
                "message.im",
                "reaction_added",
            ]
        },
        "interactivity": {"is_enabled": True},
        "org_deploy_enabled": False,
        "socket_mode_enabled": True,
        "token_rotation_enabled": False,
    },
}


def _detect_wsl() -> bool:
    """Detect if running inside WSL."""
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except Exception:
        return False


def _detect_windows_user() -> str | None:
    """Try to detect the Windows username from WSL."""
    # Method 1: Check /mnt/c/Users/ for non-system directories
    try:
        users_dir = Path("/mnt/c/Users")
        if users_dir.exists():
            skip = {"Public", "Default", "Default User", "All Users", "desktop.ini"}
            for entry in users_dir.iterdir():
                if (
                    entry.is_dir()
                    and entry.name not in skip
                    and not entry.name.startswith(".")
                ):
                    return entry.name
    except Exception:
        pass

    # Method 2: Check USERPROFILE or USERNAME env vars (sometimes set in WSL)
    for var in ("USERPROFILE", "USERNAME", "USER"):
        val = os.environ.get(var, "")
        if val and "/" not in val and "\\" not in val:
            return val
        if "\\" in val:
            return val.split("\\")[-1]

    return None


def _suggest_working_dir() -> str:
    """Suggest a default working directory based on the platform."""
    if _detect_wsl():
        win_user = _detect_windows_user()
        if win_user:
            win_path = f"/mnt/c/Users/{win_user}/Documents/Amplifier"
            if Path(f"/mnt/c/Users/{win_user}").exists():
                return win_path
        # Fallback: WSL home
        return str(Path.home() / "Documents" / "Amplifier")

    # Linux / macOS
    return str(Path.home() / "Documents" / "Amplifier")


def _generate_manifest_url() -> str:
    """Generate the one-click Slack app creation URL."""
    manifest_json = json.dumps(SLACK_MANIFEST)
    encoded = urllib.parse.quote(manifest_json)
    return f"https://api.slack.com/apps?new_app=1&manifest_json={encoded}"


def _prompt(message: str, default: str = "") -> str:
    """Prompt for input with optional default."""
    if default:
        raw = input(f"{message} [{default}]: ").strip()
        return raw if raw else default
    else:
        while True:
            raw = input(f"{message}: ").strip()
            if raw:
                return raw
            print("  (required)")


def _prompt_choice(
    message: str, choices: list[tuple[str, str]], default: int = 1
) -> str:
    """Prompt for a numbered choice. Returns the value."""
    print(f"\n{message}")
    for i, (label, _) in enumerate(choices, 1):
        marker = " (default)" if i == default else ""
        print(f"  [{i}] {label}{marker}")
    while True:
        raw = input(f"Choice [{default}]: ").strip()
        if not raw:
            return choices[default - 1][1]
        try:
            idx = int(raw)
            if 1 <= idx <= len(choices):
                return choices[idx - 1][1]
        except ValueError:
            pass
        print(f"  Please enter 1-{len(choices)}")


def run_setup() -> None:
    """Run the interactive setup wizard."""
    print()
    print("=" * 50)
    print("  Amplifier Hive Slack — Setup")
    print("=" * 50)
    print()

    # Step 1: Create Slack app
    print("Step 1: Create your Slack app")
    print()
    manifest_url = _generate_manifest_url()
    print("  Open this link to create your app with everything pre-configured:")
    print()
    print(f"  {manifest_url}")
    print()
    print("  In Slack's app creation page:")
    print("    1. Select your workspace")
    print("    2. Click 'Create'")
    print("    3. Click 'Install to Workspace' → 'Allow'")
    print()
    input("  Press Enter when you've created and installed the app...")

    # Step 2: Collect tokens
    print()
    print("Step 2: Copy your tokens")
    print()
    print("  Go to your app's settings at https://api.slack.com/apps")
    print()

    print("  OAuth & Permissions → Bot User OAuth Token:")
    bot_token = _prompt("  Bot Token (xoxb-...)")
    if not bot_token.startswith("xoxb-"):
        print("  Warning: Bot tokens usually start with 'xoxb-'")

    print()
    print("  Basic Information → App-Level Tokens → Generate Token")
    print("    Name it anything (e.g. 'socket'), add scope: connections:write")
    app_token = _prompt("  App Token (xapp-...)")
    if not app_token.startswith("xapp-"):
        print("  Warning: App tokens usually start with 'xapp-'")

    # Step 3: AI Provider
    print()
    print("Step 3: Choose your AI provider")

    provider_choice = _prompt_choice(
        "Which AI provider?",
        [
            ("Anthropic (Claude)", "anthropic"),
            ("OpenAI (GPT)", "openai"),
            ("Google (Gemini)", "gemini"),
        ],
        default=1,
    )

    env_var_map = {
        "anthropic": ("ANTHROPIC_API_KEY", "Anthropic API key"),
        "openai": ("OPENAI_API_KEY", "OpenAI API key"),
        "gemini": ("GOOGLE_API_KEY", "Google API key"),
    }

    env_var, label = env_var_map[provider_choice]

    # Check if key already exists in environment
    existing_key = os.environ.get(env_var, "")
    if existing_key:
        print(f"\n  Found existing {env_var} in your environment.")
        use_existing = input("  Use it? [Y/n]: ").strip().lower()
        if use_existing in ("", "y", "yes"):
            api_key = existing_key
        else:
            api_key = _prompt(f"  {label}")
    else:
        api_key = _prompt(f"  {label}")

    # Step 4: Working directory
    print()
    print("Step 4: Working directory")
    print("  This is where your assistant reads and writes files.")

    default_dir = _suggest_working_dir()
    if _detect_wsl():
        print("  (WSL detected — defaulting to your Windows Documents folder)")

    working_dir = _prompt("  Working directory", default_dir)

    # Create the directory if it doesn't exist
    working_path = Path(working_dir)
    if not working_path.exists():
        try:
            working_path.mkdir(parents=True, exist_ok=True)
            print(f"  Created: {working_dir}")
        except Exception as e:
            print(f"  Warning: Could not create directory: {e}")

    # Step 5: Assistant name
    print()
    print("Step 5: Name your assistant")
    assistant_name = _prompt("  Assistant name", "Amplifier")

    # Step 6: Write files
    print()
    print("Writing configuration...")

    # Write .env
    env_path = Path(".env")
    env_lines = [
        f"SLACK_APP_TOKEN={app_token}",
        f"SLACK_BOT_TOKEN={bot_token}",
        f"{env_var}={api_key}",
    ]
    env_path.write_text("\n".join(env_lines) + "\n")
    print(f"  Created: {env_path}")

    # Write config
    config_path = Path("config/my-assistant.yaml")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_content = f"""instance:
  name: assistant
  bundle: amplifier-dev
  working_dir: "{working_dir}"
  persona:
    name: "{assistant_name}"
    emoji: ":sparkles:"

slack:
  app_token: ${{SLACK_APP_TOKEN}}
  bot_token: ${{SLACK_BOT_TOKEN}}
"""
    config_path.write_text(config_content)
    print(f"  Created: {config_path}")

    # Step 7: Offer to install service
    print()
    print("=" * 50)
    print(f"  Setup complete! Your assistant '{assistant_name}' is ready.")
    print("=" * 50)
    print()
    print("To start your assistant:")
    print()
    print("  source .venv/bin/activate")
    print("  set -a; source .env; set +a")
    print("  hive-slack config/my-assistant.yaml")
    print()
    print("Or install as a background service:")
    print()
    print("  hive-slack service install config/my-assistant.yaml")
    print("  hive-slack service start")
    print()

    install_service = input("Install as a service now? [y/N]: ").strip().lower()
    if install_service in ("y", "yes"):
        from hive_slack import service_manager

        info = service_manager.install("config/my-assistant.yaml")
        print(f"  Installed: {info.message}")
        start_now = input("  Start now? [Y/n]: ").strip().lower()
        if start_now in ("", "y", "yes"):
            info = service_manager.start()
            print(f"  {info.message}")
            print()
            print("Your assistant is live! Open Slack and send it a DM.")
    else:
        print(
            "You can install it later with: "
            "hive-slack service install config/my-assistant.yaml"
        )


if __name__ == "__main__":
    run_setup()
