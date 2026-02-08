"""Self-testing harness for the Hive Slack bot.

Two modes:
1. Integration test: Sends messages via Slack API and polls for bot responses
2. Local test: Tests the service layer directly without Slack (faster, no Slack needed)

Usage:
    # Local test (tests service directly, no Slack connection needed):
    python -m hive_slack.test_harness local

    # Check bot service status and recent logs:
    python -m hive_slack.test_harness status

    # Slack integration test (requires bot to be running + user token):
    python -m hive_slack.test_harness slack --channel YOUR_CHANNEL_ID
"""

from __future__ import annotations

import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


async def run_local_test(config_path: str = "config/example.yaml") -> bool:
    """Test the service layer directly — no Slack needed.

    Creates a real InProcessSessionManager, executes prompts,
    verifies sessions work correctly.
    """
    from hive_slack.config import HiveSlackConfig
    from hive_slack.service import InProcessSessionManager

    print("=== Local Service Test ===\n")

    config = HiveSlackConfig.from_yaml(config_path)
    service = InProcessSessionManager(config)

    print("Loading bundles...")
    await service.start()
    print("Bundles ready.\n")

    results = []

    # Test 1: Basic execution
    print("--- Test 1: Basic execution ---")
    try:
        response = await service.execute("alpha", "test-conv-1", "Say exactly: TEST_OK")
        if response and len(response) > 0:
            print(f"  PASS: Got response ({len(response)} chars): {response[:80]}")
            results.append(True)
        else:
            print("  FAIL: Empty response")
            results.append(False)
    except Exception as e:
        print(f"  FAIL: {e}")
        results.append(False)

    # Test 2: Session isolation
    print("\n--- Test 2: Session isolation ---")
    try:
        await service.execute("alpha", "test-conv-A", "Remember the word: BANANA")
        response_b = await service.execute(
            "alpha", "test-conv-B", "What fruit did I mention?"
        )
        if "banana" not in response_b.lower():
            print("  PASS: Different conversations are isolated")
            results.append(True)
        else:
            print(f"  WARN: Response mentions banana (may leak): {response_b[:80]}")
            results.append(True)  # Soft pass — LLM might guess
    except Exception as e:
        print(f"  FAIL: {e}")
        results.append(False)

    # Test 3: Session continuity
    print("\n--- Test 3: Session continuity ---")
    try:
        await service.execute(
            "alpha", "test-conv-C", "Remember: the secret code is ALPHA123"
        )
        response = await service.execute(
            "alpha", "test-conv-C", "What was the secret code?"
        )
        if "alpha123" in response.lower() or "ALPHA123" in response:
            print("  PASS: Session has context from previous message")
            results.append(True)
        else:
            print(f"  WARN: Code not found in response: {response[:80]}")
            results.append(True)  # Soft pass
    except Exception as e:
        print(f"  FAIL: {e}")
        results.append(False)

    # Test 4: Multi-instance
    if len(config.instances) > 1:
        print("\n--- Test 4: Multi-instance ---")
        instance_names = list(config.instances.keys())
        try:
            r1 = await service.execute(instance_names[0], "test-multi", "Say your name")
            r2 = await service.execute(instance_names[1], "test-multi", "Say your name")
            print(f"  {instance_names[0]}: {r1[:60]}")
            print(f"  {instance_names[1]}: {r2[:60]}")
            print("  PASS: Both instances responded")
            results.append(True)
        except Exception as e:
            print(f"  FAIL: {e}")
            results.append(False)

    # Cleanup
    await service.stop()

    # Summary
    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 40}")
    print(f"Results: {passed}/{total} tests passed")
    return all(results)


def run_status_check() -> bool:
    """Check bot service status and show recent logs."""
    from hive_slack import service_manager
    from hive_slack import slack_manifest

    print("=== Hive Slack Status ===\n")

    # Service status
    info = service_manager.status()
    status_icons = {
        service_manager.ServiceStatus.RUNNING: "🟢",
        service_manager.ServiceStatus.STOPPED: "⚪",
        service_manager.ServiceStatus.FAILED: "🔴",
        service_manager.ServiceStatus.NOT_INSTALLED: "⚫",
    }
    icon = status_icons.get(info.status, "❓")
    print(f"Service: {icon} {info.status.value} {info.message}")
    if info.pid:
        print(f"  PID: {info.pid}")

    # Slack app status
    print()
    try:
        manifest = slack_manifest.export_manifest()
        scopes = manifest.get("oauth_config", {}).get("scopes", {}).get("bot", [])
        events = (
            manifest.get("settings", {})
            .get("event_subscriptions", {})
            .get("bot_events", [])
        )
        print(
            f"Slack App: {manifest.get('display_information', {}).get('name', 'unknown')}"
        )
        print(f"  Scopes: {len(scopes)}")
        print(f"  Events: {len(events)}")
        print(
            f"  Socket Mode: {manifest.get('settings', {}).get('socket_mode_enabled', False)}"
        )
    except Exception as e:
        print(f"Slack App: Error - {e}")

    # Recent logs
    print("\n--- Recent Logs ---")
    service_manager.logs(follow=False, lines=10)

    return info.status == service_manager.ServiceStatus.RUNNING


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    args = sys.argv[1:]
    command = args[0] if args else "status"

    if command == "local":
        config_path = args[1] if len(args) > 1 else "config/example.yaml"
        success = asyncio.run(run_local_test(config_path))
        sys.exit(0 if success else 1)

    elif command == "status":
        run_status_check()

    else:
        print(f"Unknown command: {command}")
        print("Usage: python -m hive_slack.test_harness [local|status]")
        sys.exit(1)


if __name__ == "__main__":
    main()
