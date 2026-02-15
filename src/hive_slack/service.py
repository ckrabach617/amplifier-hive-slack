"""In-process Amplifier session management.

This module will be replaced by a gRPC client when the Rust service exists.
The interface (execute signature) stays the same — only the implementation changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from hive_slack.config import HiveSlackConfig

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path("~/.amplifier/hive/sessions").expanduser()

# Vendored modules — bundled with this project so we don't depend on the shared
# Amplifier cache (which can be cleared by other Amplifier installations).
# service.py lives at src/hive_slack/service.py -> project root is 3 levels up.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LOOP_INTERACTIVE_SOURCE = str(_PROJECT_ROOT / "modules" / "loop-interactive")


class InProcessSessionManager:
    """Manages Amplifier sessions in-process using amplifier-core directly.

    Satisfies the SessionManager Protocol defined in slack.py.
    Will be replaced by GrpcSessionManager when Rust service exists.
    """

    def __init__(self, config: HiveSlackConfig) -> None:
        self._config = config
        self._prepared: dict[str, object] = {}  # bundle_name → PreparedBundle
        self._sessions: dict[str, object] = {}  # "instance:conv_id" → AmplifierSession
        self._locks: dict[str, asyncio.Lock] = {}
        self._approval_systems: dict[
            str, object
        ] = {}  # session_key → SlackApprovalSystem
        self._executing: set[str] = set()  # session_keys with active execute() calls
        self._pending_notifications: dict[
            str, list[str]
        ] = {}  # session_key → queued messages

    async def start(self) -> None:
        """Load and prepare bundles for all instances. Called once at startup."""
        from amplifier_foundation import Bundle, load_bundle

        # Collect unique bundles to avoid loading the same one twice
        bundles_needed: dict[str, str] = {}  # bundle_name → first instance using it
        for inst in self._config.instances.values():
            if inst.bundle not in bundles_needed:
                bundles_needed[inst.bundle] = inst.name

        provider = self._detect_provider()

        for bundle_name, first_instance in bundles_needed.items():
            logger.info("Loading bundle '%s' (used by %s)", bundle_name, first_instance)
            bundle = await load_bundle(bundle_name)

            if provider:
                logger.info(
                    "Adding provider: %s (%s)",
                    provider["module"],
                    provider["config"].get("model", "default"),
                )
                provider_bundle = Bundle(
                    name="provider-overlay",
                    version="0.0.1",
                    providers=[provider],
                )
                bundle = bundle.compose(provider_bundle)

            # Override orchestrator with loop-interactive (supports mid-execution injection)
            orchestrator_overlay = Bundle(
                name="orchestrator-overlay",
                version="0.0.1",
                session={
                    "orchestrator": {
                        "module": "loop-interactive",
                        "source": _LOOP_INTERACTIVE_SOURCE,
                        "config": {"extended_thinking": True},
                    },
                },
            )
            bundle = bundle.compose(orchestrator_overlay)
            logger.info("Using loop-interactive orchestrator (injection support)")

            # Compose recipes behavior for Tier 3 staged approval workflows
            try:
                recipes_behavior = await load_bundle(
                    "git+https://github.com/microsoft/amplifier-bundle-recipes@main"
                    "#subdirectory=behaviors/recipes.yaml"
                )
                bundle = bundle.compose(recipes_behavior)
                logger.info("Composed recipes behavior (Tier 3 approval gates)")
            except Exception:
                logger.warning("Could not load recipes behavior", exc_info=True)

            logger.info("Preparing bundle '%s'...", bundle_name)
            self._prepared[bundle_name] = await bundle.prepare()

        logger.info(
            "All bundles ready (%d bundle(s) for %d instance(s))",
            len(self._prepared),
            len(self._config.instances),
        )

    @staticmethod
    def _detect_provider() -> dict | None:
        """Auto-detect LLM provider from environment variables."""
        import os

        if os.environ.get("ANTHROPIC_API_KEY"):
            return {
                "module": "provider-anthropic",
                "source": "git+https://github.com/microsoft/amplifier-module-provider-anthropic@main",
                "config": {"model": "claude-sonnet-4-20250514"},
            }
        if os.environ.get("OPENAI_API_KEY"):
            return {
                "module": "provider-openai",
                "source": "git+https://github.com/microsoft/amplifier-module-provider-openai@main",
                "config": {"model": "gpt-4o"},
            }
        if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
            return {
                "module": "provider-gemini",
                "source": "git+https://github.com/microsoft/amplifier-module-provider-gemini@main",
                "config": {},
            }
        return None

    async def execute(
        self,
        instance_name: str,
        conversation_id: str,
        prompt: str,
        on_progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        slack_context: dict[str, Any] | None = None,
    ) -> str:
        """Execute a prompt with optional progress callback.

        Creates a new session if one doesn't exist.
        Serializes execution per-session (sessions are not reentrant).

        The on_progress callback, if provided, is called with:
            ("executing", {"prompt": <first 100 chars>}) — before execution
            ("complete", {"status": "success"})           — after success
            ("error", {"error": "execution failed"})      — after failure
        """
        if not self._prepared:
            raise RuntimeError("SessionManager not started — call start() first")

        session_key = f"{instance_name}:{conversation_id}"
        lock = self._locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            session = await self._get_or_create_session(
                instance_name, conversation_id, slack_context=slack_context
            )

            # Drain queued worker notifications
            pending = self._pending_notifications.pop(session_key, [])
            if pending:
                notification_block = "\n".join(pending)
                prompt = (
                    f"[WORKER REPORTS]\n{notification_block}\n"
                    f"[END WORKER REPORTS]\n\n{prompt}"
                )

            logger.info(
                "Executing for %s in %s: %s",
                instance_name,
                conversation_id,
                prompt[:80],
            )

            if on_progress:
                try:
                    await on_progress("executing", {"prompt": prompt[:100]})
                except Exception:
                    pass

            # Register temporary hooks to forward tool events to on_progress
            unregister_hooks: list[Callable[[], None]] = []
            if on_progress:
                unregister_hooks = self._register_progress_hooks(session, on_progress)

            self._executing.add(session_key)
            try:
                response = await session.execute(prompt)
            except Exception:
                if on_progress:
                    try:
                        await on_progress("error", {"error": "execution failed"})
                    except Exception:
                        pass
                raise
            finally:
                self._executing.discard(session_key)
                # Unregister temporary hooks
                for unreg in unregister_hooks:
                    try:
                        unreg()
                    except Exception:
                        pass

            if on_progress:
                try:
                    await on_progress("complete", {"status": "success"})
                except Exception:
                    pass

            # Persist transcript after each turn (best-effort)
            await self._save_transcript(instance_name, conversation_id, session)

            return response

    def _register_progress_hooks(
        self,
        session: object,
        callback: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> list[Callable[[], None]]:
        """Register temporary hooks on the session to forward tool events to the progress callback.

        Returns a list of unregister functions to call when done.
        """
        coordinator = getattr(session, "coordinator", None)
        if coordinator is None:
            return []

        hooks = coordinator.get("hooks") if hasattr(coordinator, "get") else None
        if hooks is None or not hasattr(hooks, "register"):
            return []

        from amplifier_core.models import HookResult

        unregister_fns: list[Callable[[], None]] = []

        async def on_tool_pre(event_name: str, data: dict[str, Any]) -> HookResult:
            tool_name = data.get("tool_name", "")
            logger.info("Progress hook fired: tool:pre → %s", tool_name)

            payload: dict[str, Any] = {"tool": tool_name}

            # Extract delegate agent name for richer status
            if tool_name == "delegate":
                tool_input = data.get("tool_input", {})
                if isinstance(tool_input, str):
                    import json as _json

                    try:
                        tool_input = _json.loads(tool_input)
                    except Exception:
                        tool_input = {}
                if isinstance(tool_input, dict):
                    agent = tool_input.get("agent", "")
                    if agent:
                        payload["agent"] = agent

            try:
                await callback("tool:start", payload)
            except Exception:
                logger.warning("Progress callback error for tool:start", exc_info=True)
            return HookResult(action="continue")

        async def on_tool_post(event_name: str, data: dict[str, Any]) -> HookResult:
            tool_name = data.get("tool_name", "")
            logger.info("Progress hook fired: tool:post → %s", tool_name)

            payload: dict[str, Any] = {"tool": tool_name}

            # Extract todo data when the todo tool is called
            if tool_name == "todo":
                tool_input = data.get("tool_input", {})
                if isinstance(tool_input, str):
                    import json as _json

                    try:
                        tool_input = _json.loads(tool_input)
                    except Exception:
                        tool_input = {}
                if isinstance(tool_input, dict):
                    todos = tool_input.get("todos")
                    if todos is None:
                        # Fallback: check result output (for "list" action)
                        result = data.get("result", {})
                        output = (
                            result.get("output", {}) if isinstance(result, dict) else {}
                        )
                        todos = (
                            output.get("todos") if isinstance(output, dict) else None
                        )
                    if todos:
                        payload["todos"] = todos

            try:
                await callback("tool:end", payload)
            except Exception:
                logger.warning("Progress callback error for tool:end", exc_info=True)
            return HookResult(action="continue")

        try:
            unreg = hooks.register(
                "tool:pre", on_tool_pre, priority=999, name="_progress_pre"
            )
            unregister_fns.append(unreg)
            logger.info("Registered progress hook: tool:pre")
        except Exception:
            logger.warning("Could not register tool:pre progress hook", exc_info=True)

        try:
            unreg = hooks.register(
                "tool:post", on_tool_post, priority=999, name="_progress_post"
            )
            unregister_fns.append(unreg)
            logger.info("Registered progress hook: tool:post")
        except Exception:
            logger.warning("Could not register tool:post progress hook", exc_info=True)

        return unregister_fns

    def inject_message(
        self, instance_name: str, conversation_id: str, content: str
    ) -> bool:
        """Inject a user message into a running session's orchestrator.

        Returns True if the message was injected, False if the session
        doesn't exist or doesn't support injection.
        """
        session_key = f"{instance_name}:{conversation_id}"
        session = self._sessions.get(session_key)
        if session is None:
            return False

        coordinator = getattr(session, "coordinator", None)
        if coordinator is None:
            return False

        orchestrator = (
            coordinator.get("orchestrator") if hasattr(coordinator, "get") else None
        )
        if orchestrator is None:
            return False

        if hasattr(orchestrator, "inject_message"):
            orchestrator.inject_message(content)
            logger.info(
                "Injected message into %s:%s: %s",
                instance_name,
                conversation_id,
                content[:80],
            )
            return True

        return False

    def notify(self, instance_name: str, conversation_id: str, message: str) -> bool:
        """Deliver a notification to a session.

        If the session is actively executing, injects the message into the
        orchestrator loop for immediate processing. Otherwise, queues the
        message to be prepended to the next execute() prompt.

        Returns True if delivered immediately, False if queued.
        """
        session_key = f"{instance_name}:{conversation_id}"

        if session_key in self._executing:
            if self.inject_message(instance_name, conversation_id, message):
                return True  # Delivered to active orchestrator loop

        # Either not executing, or inject failed — queue for next execute()
        self._pending_notifications.setdefault(session_key, []).append(message)
        return False

    async def _get_or_create_session(
        self,
        instance_name: str,
        conversation_id: str,
        slack_context: dict[str, Any] | None = None,
    ):
        """Get existing session or create a new one."""
        session_key = f"{instance_name}:{conversation_id}"
        if session_key not in self._sessions:
            instance = self._config.get_instance(instance_name)
            prepared = self._prepared.get(instance.bundle)
            if prepared is None:
                raise RuntimeError(
                    f"No prepared bundle for '{instance.bundle}' "
                    f"(instance '{instance_name}')"
                )

            working_dir = Path(instance.working_dir).expanduser()
            working_dir.mkdir(parents=True, exist_ok=True)

            logger.info(
                "Creating session for %s in %s (bundle=%s, cwd=%s)",
                instance_name,
                conversation_id,
                instance.bundle,
                working_dir,
            )

            # Create Slack-specific systems if context provided
            approval_system = None
            display_system = None

            if slack_context:
                from hive_slack.approval import SlackApprovalSystem
                from hive_slack.display import SlackDisplaySystem

                client = slack_context.get("client")
                channel = slack_context.get("channel", "")
                thread_ts = slack_context.get("thread_ts", "")

                approval_system = SlackApprovalSystem(client, channel, thread_ts)
                display_system = SlackDisplaySystem(client, channel, thread_ts)

                # Store approval system so connector can resolve button clicks
                self._approval_systems[session_key] = approval_system

            session = await prepared.create_session(
                session_cwd=working_dir,
                approval_system=approval_system,
                display_system=display_system,
            )

            # Mount Slack tools post-creation
            if slack_context:
                from hive_slack.tools import create_slack_tools
                from hive_slack.dispatch import DispatchWorkerTool

                client = slack_context["client"]
                channel = slack_context.get("channel", "")
                thread_ts = slack_context.get("thread_ts", "")
                user_ts = slack_context.get("user_ts", "")

                tools = create_slack_tools(client, channel, thread_ts, user_ts)

                # Add dispatch_worker tool for background task execution
                tools.append(
                    DispatchWorkerTool(
                        session_manager=self,
                        instance_name=instance_name,
                        working_dir=instance.working_dir,
                        director_conversation_id=conversation_id,
                    )
                )

                for tool in tools:
                    try:
                        await session.coordinator.mount("tools", tool)
                    except Exception:
                        logger.debug(
                            "Could not mount tool %s",
                            getattr(tool, "name", "?"),
                            exc_info=True,
                        )

            self._sessions[session_key] = session
        return self._sessions[session_key]

    async def _save_transcript(
        self,
        instance_name: str,
        conversation_id: str,
        session: object,
    ) -> None:
        """Save session transcript to JSONL file. Best-effort — never raises."""
        try:
            # Get messages from the context manager
            coordinator = getattr(session, "coordinator", None)
            if coordinator is None:
                return
            context = (
                coordinator.get("context") if hasattr(coordinator, "get") else None
            )
            if context is None or not hasattr(context, "get_messages"):
                return

            messages = context.get_messages()
            # get_messages may be async (returns coroutine) — handle both
            if asyncio.iscoroutine(messages):
                messages = await messages
            if not messages:
                return

            session_dir = (
                SESSIONS_DIR / instance_name / conversation_id.replace(":", "_")
            )
            session_dir.mkdir(parents=True, exist_ok=True)

            # Write transcript as JSONL
            transcript_path = session_dir / "transcript.jsonl"
            with open(transcript_path, "w") as f:
                for msg in messages:
                    if isinstance(msg, dict):
                        f.write(json.dumps(msg) + "\n")

            # Write metadata
            metadata = {
                "instance": instance_name,
                "conversation_id": conversation_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "turn_count": len(
                    [
                        m
                        for m in messages
                        if isinstance(m, dict) and m.get("role") == "user"
                    ]
                ),
            }
            metadata_path = session_dir / "metadata.json"
            with open(metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)

        except Exception:
            logger.warning(
                "Failed to save transcript for %s:%s",
                instance_name,
                conversation_id,
                exc_info=True,
            )

    def get_approval_system(
        self, instance_name: str, conversation_id: str
    ) -> object | None:
        """Get the approval system for a session (for resolving button clicks)."""
        session_key = f"{instance_name}:{conversation_id}"
        return self._approval_systems.get(session_key)

    async def stop(self) -> None:
        """Cleanup all sessions."""
        logger.info(
            "Stopping session manager, cleaning up %d sessions",
            len(self._sessions),
        )
        for key, session in list(self._sessions.items()):
            try:
                await session.cleanup()
            except Exception:
                logger.exception("Error cleaning up session %s", key)
        self._sessions.clear()
        self._locks.clear()
        self._approval_systems.clear()
