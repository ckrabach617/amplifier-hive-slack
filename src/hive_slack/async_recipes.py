"""Non-blocking wrapper for the recipes tool.

When the Director calls ``recipes(operation="execute", ...)``, the stock tool
blocks the orchestrator loop until the entire recipe finishes -- which can take
minutes for Tier 3 workflows with approval gates.  This wrapper makes long
operations (execute, resume) non-blocking by dispatching them to background
asyncio tasks tracked by the shared WorkerManager.

Quick operations (list, validate, approvals, approve, deny, cancel) pass
through synchronously because they return instantly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from hive_slack.worker_manager import WorkerManager

logger = logging.getLogger(__name__)


class AsyncRecipesTool:
    """Non-blocking proxy for the recipes tool.

    Constructed in ``service.py`` after session creation.  The original
    recipes tool is found via ``coordinator.get("tools")`` and handed
    to this wrapper, which is then mounted in its place (same ``name``).
    """

    # Operations that block for a long time (recipe execution loop)
    _ASYNC_OPS: frozenset[str] = frozenset({"execute", "resume"})

    def __init__(
        self,
        wrapped_tool: Any,
        worker_manager: WorkerManager,
        notify_fn: Callable[[str], None],
        slack_post_fn: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._wrapped = wrapped_tool
        self._workers = worker_manager
        self._notify_queue = notify_fn
        self._slack_post_fn = slack_post_fn
        self._counter = 0

    # -- Tool protocol ---------------------------------------------------------

    @property
    def name(self) -> str:
        return "recipes"

    @property
    def description(self) -> str:
        return getattr(self._wrapped, "description", "Manage recipe workflows")

    @property
    def input_schema(self) -> dict:
        return getattr(self._wrapped, "input_schema", {})

    # -- Helpers ---------------------------------------------------------------

    async def _post(self, message: str) -> None:
        """Post a notification directly to Slack, falling back to queue.

        When a ``slack_post_fn`` was provided (i.e. we have Slack
        context), the message is posted to the channel immediately so
        the user doesn't have to send another message to see it.
        Otherwise we fall back to the queue which drains on next
        ``execute()`` call.
        """
        if self._slack_post_fn is not None:
            try:
                await self._slack_post_fn(message)
                return
            except Exception:
                logger.warning("Direct Slack post failed, falling back to queue")
        self._notify_queue(message)

    # -- Execution -------------------------------------------------------------

    async def execute(self, input: dict[str, Any]) -> Any:
        """Dispatch long ops to background; pass quick ops through."""
        from amplifier_core.models import ToolResult

        operation = input.get("operation", "")

        # Quick operations -- delegate directly
        if operation not in self._ASYNC_OPS:
            return await self._wrapped.execute(input)

        # Long operations -- background dispatch
        self._counter += 1
        recipe_path = input.get("recipe_path", "")
        session_id = input.get("session_id", "")
        label = recipe_path.rsplit("/", 1)[-1] if recipe_path else session_id
        if not label:
            label = f"recipe-{self._counter}"
        task_id = f"recipe-{self._counter}"

        async def _run() -> None:
            try:
                result = await self._wrapped.execute(input)
                output = getattr(result, "output", None) or str(result)
                # Truncate for the notification (full output in recipe session)
                if len(output) > 500:
                    output = output[:500] + "... [truncated]"
                await self._post(f"[RECIPE COMPLETE] {label}\n{output}")
            except asyncio.CancelledError:
                await self._post(f"[RECIPE CANCELLED] {label}")
            except Exception as e:
                logger.exception("Background recipe failed: %s", task_id)
                await self._post(f"[RECIPE FAILED] {label}\nError: {e}")

        task = asyncio.create_task(_run(), name=task_id)
        self._workers.register(task_id, task, description=f"Recipe: {label}", tier="3")

        return ToolResult(
            success=True,
            output=(
                f"Recipe '{label}' started in background ({task_id}). "
                "You'll be notified when it completes or needs approval. "
                "Approval buttons will appear in Slack automatically. "
                "STOP. Do NOT call any more tools. Respond to the user NOW."
            ),
        )
