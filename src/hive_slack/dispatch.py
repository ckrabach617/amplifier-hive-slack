"""Background worker dispatch for The Director pattern.

Provides a tool that lets The Director kick off long-running work
in a background session. The Director returns immediately; the worker
writes results to TASKS.md. The Director reads TASKS.md to report
back when asked.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from hive_slack.task_store import TaskStore

logger = logging.getLogger(__name__)


class DispatchWorkerTool:
    """Dispatch a task to a background worker session.

    The Director calls this to hand off Tier 2+ work. The tool returns
    immediately with a confirmation. The worker runs in a background
    asyncio task and writes results to TASKS.md when done. Nothing is
    posted to the channel -- The Director reports when asked.
    """

    def __init__(
        self,
        session_manager,
        instance_name: str,
        working_dir: str,
        director_conversation_id: str = "",
    ) -> None:
        self._manager = session_manager
        self._instance_name = instance_name
        self._working_dir = Path(working_dir).expanduser()
        self._director_conversation_id = director_conversation_id
        self._worker_counter = 0
        self._store = TaskStore(self._working_dir / "TASKS.md")

    @property
    def name(self) -> str:
        return "dispatch_worker"

    @property
    def description(self) -> str:
        return (
            "Dispatch a task to a background worker. Use for Tier 2+ work that takes "
            "more than a few seconds. The worker runs independently and writes results "
            "to TASKS.md when done. IMPORTANT: After calling this tool, respond to the "
            "user IMMEDIATELY. Do NOT read files, call other tools, or do any more work. "
            "Just confirm the dispatch and ask what else they need."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Complete task description for the worker. Must be self-contained "
                        "-- include all context the worker needs. The worker cannot see "
                        "this conversation."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Short identifier for this task (e.g., 'deck-stain-research'). "
                        "Used in TASKS.md tracking."
                    ),
                },
            },
            "required": ["task", "task_id"],
        }

    async def execute(self, input: dict[str, Any]) -> Any:
        """Dispatch a background worker and return immediately."""
        from amplifier_core.models import ToolResult

        task = input.get("task", "")
        task_id = input.get("task_id", "")

        if not task:
            return ToolResult(success=False, output="No task provided")
        if not task_id:
            return ToolResult(success=False, output="No task_id provided")

        self._worker_counter += 1

        # Add to TASKS.md as Active immediately
        await self._store.add_active(task_id, task)

        # Launch background task
        asyncio.create_task(
            self._run_worker(task, task_id),
            name=f"worker-{task_id}",
        )

        return ToolResult(
            success=True,
            output=(
                f"Worker dispatched: {task_id}. TASKS.md updated. "
                "STOP. Do NOT call any more tools. Respond to the user NOW -- "
                "confirm what you dispatched and ask what else they need."
            ),
        )

    async def _run_worker(self, task: str, task_id: str) -> None:
        """Run worker session in background and write result to TASKS.md."""
        conversation_id = f"worker:{task_id}:{self._worker_counter}"

        try:
            logger.info("Background worker starting: %s", task_id)

            response = await self._manager.execute(
                self._instance_name,
                conversation_id,
                task,
            )

            # Write result to TASKS.md (truncate long responses for the summary)
            summary = response.strip()
            if len(summary) > 500:
                summary = (
                    summary[:500] + "... [truncated -- ask Director for full result]"
                )

            await self._store.complete_task(task_id, summary)
            logger.info("Background worker completed: %s", task_id)

            # Notify Director of completion
            self._manager.notify(
                self._instance_name,
                self._director_conversation_id,
                f'[WORKER REPORT] Task "{task_id}" completed.\n'
                f"Result: {summary}\n"
                "Full details in TASKS.md.",
            )

        except Exception as e:
            logger.exception("Background worker failed: %s", task_id)
            await self._store.fail_task(task_id, str(e))

            # Notify Director of failure
            self._manager.notify(
                self._instance_name,
                self._director_conversation_id,
                f'[WORKER REPORT] Task "{task_id}" FAILED.\nError: {e}',
            )
