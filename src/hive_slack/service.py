"""In-process Amplifier session management.

This module will be replaced by a gRPC client when the Rust service exists.
The interface (execute signature) stays the same — only the implementation changes.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from hive_slack.config import HiveSlackConfig

logger = logging.getLogger(__name__)


class InProcessSessionManager:
    """Manages Amplifier sessions in-process using amplifier-core directly.

    Satisfies the SessionManager Protocol defined in slack.py.
    Will be replaced by GrpcSessionManager when Rust service exists.
    """

    def __init__(self, config: HiveSlackConfig) -> None:
        self._config = config
        self._prepared = None  # PreparedBundle, set during start()
        self._sessions: dict[str, object] = {}  # conversation_id → AmplifierSession
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        """Load and prepare the Amplifier bundle. Called once at startup."""
        from amplifier_foundation import Bundle, load_bundle

        logger.info("Loading bundle: %s", self._config.instance.bundle)
        bundle = await load_bundle(self._config.instance.bundle)

        # The foundation bundle has no provider — compose one in.
        # Auto-detect from environment: prefer Anthropic, fall back to OpenAI.
        provider = self._detect_provider()
        if provider:
            logger.info("Adding provider: %s (%s)", provider["module"], provider["config"]["model"])
            provider_bundle = Bundle(
                name="provider-overlay",
                version="0.0.1",
                providers=[provider],
            )
            bundle = bundle.compose(provider_bundle)
        else:
            logger.warning("No provider API key found in environment")

        logger.info("Preparing bundle (this may take a moment)...")
        self._prepared = await bundle.prepare()
        logger.info("Bundle ready")

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
        self, instance_name: str, conversation_id: str, prompt: str
    ) -> str:
        """Execute a prompt in the session for this conversation.

        Creates a new session if one doesn't exist for the conversation_id.
        Serializes execution per-session (sessions are not reentrant).
        """
        if self._prepared is None:
            raise RuntimeError("SessionManager not started — call start() first")

        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            session = await self._get_or_create_session(conversation_id)
            logger.info(
                "Executing in session %s for %s: %s",
                conversation_id,
                instance_name,
                prompt[:80],
            )
            response = await session.execute(prompt)
            return response

    async def _get_or_create_session(self, conversation_id: str):
        """Get existing session or create a new one for this conversation."""
        if conversation_id not in self._sessions:
            working_dir = Path(self._config.instance.working_dir).expanduser()
            working_dir.mkdir(parents=True, exist_ok=True)

            logger.info("Creating new session for conversation: %s", conversation_id)
            session = await self._prepared.create_session(
                session_cwd=working_dir,
            )
            self._sessions[conversation_id] = session
        return self._sessions[conversation_id]

    async def stop(self) -> None:
        """Cleanup all sessions."""
        logger.info(
            "Stopping session manager, cleaning up %d sessions", len(self._sessions)
        )
        for conv_id, session in list(self._sessions.items()):
            try:
                await session.cleanup()
            except Exception:
                logger.exception("Error cleaning up session %s", conv_id)
        self._sessions.clear()
        self._locks.clear()
