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
                    provider["config"]["model"],
                )
                provider_bundle = Bundle(
                    name="provider-overlay",
                    version="0.0.1",
                    providers=[provider],
                )
                bundle = bundle.compose(provider_bundle)

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
        return None

    async def execute(
        self,
        instance_name: str,
        conversation_id: str,
        prompt: str,
        on_progress: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
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
            session = await self._get_or_create_session(instance_name, conversation_id)
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

            try:
                response = await session.execute(prompt)
            except Exception:
                if on_progress:
                    try:
                        await on_progress("error", {"error": "execution failed"})
                    except Exception:
                        pass
                raise

            if on_progress:
                try:
                    await on_progress("complete", {"status": "success"})
                except Exception:
                    pass

            # Persist transcript after each turn (best-effort)
            await self._save_transcript(instance_name, conversation_id, session)

            return response

    async def _get_or_create_session(self, instance_name: str, conversation_id: str):
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
            session = await prepared.create_session(session_cwd=working_dir)
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
