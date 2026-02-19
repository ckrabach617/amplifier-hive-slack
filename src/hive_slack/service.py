"""In-process Amplifier session management.

This module will be replaced by a gRPC client when the Rust service exists.
The interface (execute signature) stays the same — only the implementation changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
        self._recipes_available: bool = True  # Set False if recipe loading fails
        self._capability_warned: set[str] = set()  # session_keys already warned
        self._started_at: float | None = None

        # Worker lifecycle manager -- shared across all dispatch tools
        from hive_slack.worker_manager import WorkerManager

        self._worker_manager = WorkerManager(timeout=600.0)  # 10 min default
        self._watchdog_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Load and prepare bundles for all instances. Called once at startup."""
        self._started_at = time.monotonic()
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
                        "config": {
                            "extended_thinking": True,
                            "force_respond_tools": [
                                "dispatch_worker",
                                "recipes",
                            ],
                        },
                    },
                },
            )
            bundle = bundle.compose(orchestrator_overlay)
            logger.info("Using loop-interactive orchestrator (injection support)")

            # Compose agents behavior for agent delegation (required by recipes)
            try:
                agents_behavior = await load_bundle(
                    "git+https://github.com/microsoft/amplifier-foundation@main"
                    "#subdirectory=behaviors/agents.yaml"
                )
                bundle = bundle.compose(agents_behavior)
                logger.info("Composed agents behavior (delegate tool)")
            except Exception:
                logger.warning(
                    "Could not load agents behavior. "
                    "Agent delegation will not be available.",
                    exc_info=True,
                )

            # Compose superpowers bundle for Tier 3 recipe agents
            # (brainstormer, implementer, spec-reviewer, etc.)
            try:
                superpowers_bundle = await load_bundle(
                    "git+https://github.com/microsoft/amplifier-bundle-superpowers@main"
                )
                bundle = bundle.compose(superpowers_bundle)
                logger.info("Composed superpowers bundle (recipe agents)")
            except Exception:
                logger.warning(
                    "Could not load superpowers bundle. "
                    "Superpowers recipe agents will not be available.",
                    exc_info=True,
                )

            # Compose recipes behavior for Tier 3 staged approval workflows
            try:
                recipes_behavior = await load_bundle(
                    "git+https://github.com/microsoft/amplifier-bundle-recipes@main"
                    "#subdirectory=behaviors/recipes.yaml"
                )
                bundle = bundle.compose(recipes_behavior)
                logger.info("Composed recipes behavior (Tier 3 approval gates)")
            except Exception:
                logger.error(
                    "TIER 3 UNAVAILABLE: Could not load recipes behavior. "
                    "Staged approval workflows will not work. "
                    "The Director will be notified on first interaction.",
                    exc_info=True,
                )
                self._recipes_available = False

            logger.info("Preparing bundle '%s'...", bundle_name)
            self._prepared[bundle_name] = await bundle.prepare()

        logger.info(
            "All bundles ready (%d bundle(s) for %d instance(s))",
            len(self._prepared),
            len(self._config.instances),
        )

        # Start the worker timeout watchdog
        self._watchdog_task = asyncio.create_task(
            self._worker_manager.run_timeout_watchdog()
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

            # One-time capability warning if recipes failed to load
            if (
                not self._recipes_available
                and session_key not in self._capability_warned
            ):
                self._capability_warned.add(session_key)
                prompt = (
                    "[SYSTEM NOTE] Tier 3 (staged approval recipes) is "
                    "unavailable this session -- the recipes bundle failed "
                    "to load at startup. You can still handle Tier 1, 1.5, "
                    "2, and 2+ requests normally. If a user requests Tier 3 "
                    "work, let them know it's temporarily unavailable.\n"
                    "[END SYSTEM NOTE]\n\n" + prompt
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

            # Register tier classification tracking hook
            tier_unreg = self._register_tier_tracking_hook(session, session_key)
            if tier_unreg:
                unregister_hooks.append(tier_unreg)

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

    def _register_tier_tracking_hook(
        self,
        session: object,
        session_key: str,
    ) -> Callable[[], None] | None:
        """Register a hook to log tier classification for each request.

        Captures dispatch_worker calls to log the declared tier. If no dispatch
        happens during execution, the request is logged as inline (Tier 1/1.5).
        Returns an unregister function, or None if hooks aren't available.
        """
        coordinator = getattr(session, "coordinator", None)
        if coordinator is None:
            return None

        hooks = coordinator.get("hooks") if hasattr(coordinator, "get") else None
        if hooks is None or not hasattr(hooks, "register"):
            return None

        from amplifier_core.models import HookResult

        dispatched_tiers: list[dict[str, str]] = []

        async def on_dispatch_pre(event_name: str, data: dict[str, Any]) -> HookResult:
            tool_name = data.get("tool_name", "")
            if tool_name == "dispatch_worker":
                tool_input = data.get("tool_input", {})
                if isinstance(tool_input, str):
                    import json as _json

                    try:
                        tool_input = _json.loads(tool_input)
                    except Exception:
                        tool_input = {}
                if isinstance(tool_input, dict):
                    dispatched_tiers.append(
                        {
                            "tier": tool_input.get("tier", "unknown"),
                            "task_id": tool_input.get("task_id", ""),
                        }
                    )
            return HookResult(action="continue")

        # Use orchestrator:complete event to log classification summary
        async def on_execution_complete(
            event_name: str, data: dict[str, Any]
        ) -> HookResult:
            if dispatched_tiers:
                for dispatch in dispatched_tiers:
                    logger.info(
                        "TIER_CLASSIFICATION session=%s tier=%s task_id=%s type=dispatched",
                        session_key,
                        dispatch["tier"],
                        dispatch["task_id"],
                    )
            else:
                logger.info(
                    "TIER_CLASSIFICATION session=%s type=inline",
                    session_key,
                )
            return HookResult(action="continue")

        unreg_fns: list[Callable[[], None]] = []
        try:
            unreg = hooks.register(
                "tool:pre", on_dispatch_pre, priority=998, name="_tier_tracking"
            )
            unreg_fns.append(unreg)
        except Exception:
            logger.debug("Could not register tier tracking hook", exc_info=True)

        try:
            unreg = hooks.register(
                "orchestrator:complete",
                on_execution_complete,
                priority=998,
                name="_tier_summary",
            )
            unreg_fns.append(unreg)
        except Exception:
            logger.debug("Could not register tier summary hook", exc_info=True)

        if not unreg_fns:
            return None

        def unregister_all() -> None:
            for fn in unreg_fns:
                try:
                    fn()
                except Exception:
                    pass

        return unregister_all

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

        Always queues the message to be prepended to the next execute() prompt.
        Worker reports should never be injected mid-execution because they can
        hijack the orchestrator loop (e.g., triggering injection point 2 after
        a force-respond cycle, causing the Director to loop instead of posting
        its response to Slack).

        Returns False (always queued, never delivered immediately).
        """
        self._pending_notifications.setdefault(
            f"{instance_name}:{conversation_id}", []
        ).append(message)
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

            # Register session.spawn capability for agent delegation & recipes
            async def spawn_capability(
                agent_name: str,
                instruction: str,
                parent_session: object,
                agent_configs: dict[str, dict[str, Any]],
                sub_session_id: str | None = None,
                orchestrator_config: dict[str, Any] | None = None,
                parent_messages: list[dict[str, Any]] | None = None,
                provider_preferences: list[Any] | None = None,
                self_delegation_depth: int = 0,
                **kwargs: Any,
            ) -> dict[str, Any]:
                """Spawn a sub-session for agent delegation."""
                from amplifier_foundation import Bundle, load_bundle

                def _bundle_from_config(name: str, config: dict) -> Bundle:
                    return Bundle(
                        name=name,
                        version="1.0.0",
                        session=config.get("session", {}),
                        providers=config.get("providers", []),
                        tools=config.get("tools", []),
                        hooks=config.get("hooks", []),
                        instruction=config.get("instruction")
                        or config.get("system", {}).get("instruction"),
                    )

                child_bundle: Bundle | None = None

                # 1. Check caller-provided agent configs
                if agent_name in agent_configs:
                    child_bundle = _bundle_from_config(
                        agent_name, agent_configs[agent_name]
                    )

                # 2. Check prepared bundle's agent registry
                elif hasattr(prepared, "bundle") and agent_name in getattr(
                    prepared.bundle, "agents", {}
                ):
                    child_bundle = _bundle_from_config(
                        agent_name, prepared.bundle.agents[agent_name]
                    )

                # 3. Dynamic resolution -- load from cached bundles
                #    Handles "namespace:agent" refs (e.g. superpowers:brainstormer)
                if child_bundle is None:
                    try:
                        child_bundle = await load_bundle(agent_name)
                        logger.info(
                            "Dynamically loaded agent bundle '%s'",
                            agent_name,
                        )
                    except Exception:
                        available = list(agent_configs.keys()) + list(
                            getattr(
                                getattr(prepared, "bundle", None),
                                "agents",
                                {},
                            ).keys()
                        )
                        raise ValueError(
                            f"Agent '{agent_name}' not found locally "
                            f"or via dynamic bundle loading. "
                            f"Local agents: {available}"
                        )

                return await prepared.spawn(
                    child_bundle=child_bundle,
                    instruction=instruction,
                    session_id=sub_session_id,
                    parent_session=parent_session,
                    orchestrator_config=orchestrator_config,
                    parent_messages=parent_messages,
                    provider_preferences=provider_preferences,
                    self_delegation_depth=self_delegation_depth,
                )

            session.coordinator.register_capability("session.spawn", spawn_capability)

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
                        worker_manager=self._worker_manager,
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

    def resolve_approval(self, action_id: str, value: str) -> bool:
        """Try to resolve an approval action across all active sessions.

        Called by the connector when a Slack block_actions event arrives.
        Scans all active approval systems to find one matching the action_id.
        Returns True if a pending approval matched, False otherwise.
        """
        for session_key, approval in self._approval_systems.items():
            if hasattr(approval, "resolve_approval") and approval.resolve_approval(
                action_id, value
            ):
                logger.info("Approval resolved for session %s", session_key)
                return True
        return False

    def get_status(
        self,
        queued_message_count: int = 0,
        connection_health: dict | None = None,
    ) -> dict:
        """Collect system health snapshot for /status command.

        Args:
            queued_message_count: Total queued messages (from SlackConnector).
            connection_health: Dict with started_at, last_health_check_at,
                reconnect_count from SlackConnection properties.

        Returns:
            Dict with uptime, recipes, workers, sessions, connection status.
            Each section degrades gracefully on error.
        """
        now = time.monotonic()

        # Uptime
        uptime = now - self._started_at if self._started_at is not None else None

        # Workers
        workers: list[dict] = []
        try:
            for info in self._worker_manager.get_active():
                workers.append(
                    {
                        "task_id": info.task_id,
                        "description": info.description,
                        "tier": info.tier,
                        "elapsed_seconds": now - info.started_at,
                    }
                )
        except Exception:
            logger.warning("Could not collect worker status", exc_info=True)

        # Sessions and executions
        try:
            sessions_count = len(self._sessions)
        except Exception:
            sessions_count = 0

        try:
            executing_count = len(self._executing)
        except Exception:
            executing_count = 0

        # Connection health
        conn: dict = {
            "status": "unknown",
            "seconds_since_last_check": None,
            "reconnect_count": 0,
        }
        if connection_health is not None:
            try:
                last_check = connection_health.get("last_health_check_at")
                if last_check is not None:
                    conn["status"] = "healthy"
                    conn["seconds_since_last_check"] = now - last_check
                elif connection_health.get("started_at") is not None:
                    conn["status"] = "starting"
                conn["reconnect_count"] = connection_health.get("reconnect_count", 0)
            except Exception:
                conn = {
                    "status": "unavailable",
                    "seconds_since_last_check": None,
                    "reconnect_count": 0,
                }

        return {
            "uptime_seconds": uptime,
            "recipes_available": self._recipes_available,
            "workers": workers,
            "sessions_count": sessions_count,
            "executing_count": executing_count,
            "queued_message_count": queued_message_count,
            "connection": conn,
        }

    async def stop(self) -> None:
        """Cleanup all sessions."""
        logger.info(
            "Stopping session manager, cleaning up %d sessions",
            len(self._sessions),
        )
        # Stop the timeout watchdog first
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

        # Cancel all active workers before tearing down sessions
        active = self._worker_manager.get_active()
        if active:
            logger.info("Cancelling %d active worker(s)...", len(active))
            await self._worker_manager.cancel_all()

        for key, session in list(self._sessions.items()):
            try:
                await session.cleanup()
            except Exception:
                logger.exception("Error cleaning up session %s", key)
        self._sessions.clear()
        self._locks.clear()
        self._approval_systems.clear()
        self._capability_warned.clear()
