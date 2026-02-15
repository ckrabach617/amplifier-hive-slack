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
        self._add_task_active(task_id, task)

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

    def _add_task_active(self, task_id: str, description: str) -> None:
        """Add a task to the Active section of TASKS.md."""
        from datetime import date

        tasks_path = self._working_dir / "TASKS.md"
        try:
            content = tasks_path.read_text() if tasks_path.exists() else ""
            entry = (
                f"- id: {task_id}\n"
                f"  description: {description[:200]}\n"
                f"  started: {date.today().isoformat()}\n"
                f"  status: worker dispatched\n"
            )
            # Insert after ## Active heading
            if "## Active" in content:
                content = content.replace("## Active\n", f"## Active\n{entry}\n", 1)
            else:
                content = f"## Active\n{entry}\n{content}"
            tasks_path.write_text(content)
            logger.info("Added %s to TASKS.md Active", task_id)
        except Exception:
            logger.warning("Could not update TASKS.md for %s", task_id, exc_info=True)

    def _complete_task(self, task_id: str, summary: str) -> None:
        """Move a task from Active to Done in TASKS.md with a summary."""
        from datetime import date

        tasks_path = self._working_dir / "TASKS.md"
        try:
            content = tasks_path.read_text() if tasks_path.exists() else ""

            # Remove from Active section (find the entry block)
            lines = content.split("\n")
            new_lines = []
            skip = False
            for line in lines:
                if line.strip().startswith(f"- id: {task_id}"):
                    skip = True
                    continue
                if skip and line.strip().startswith("- id: "):
                    skip = False
                if skip and (
                    line.strip().startswith("description:")
                    or line.strip().startswith("started:")
                    or line.strip().startswith("status:")
                ):
                    continue
                skip = False
                new_lines.append(line)

            content = "\n".join(new_lines)

            # Add to Done section
            done_entry = (
                f"- id: {task_id}\n"
                f"  completed: {date.today().isoformat()}\n"
                f"  summary: {summary}\n"
            )
            if "## Done" in content:
                # Find "## Done" with any suffix (e.g., "## Done (last 30 days)")
                for marker in ["## Done (last 30 days)", "## Done"]:
                    if marker in content:
                        content = content.replace(
                            marker + "\n", f"{marker}\n{done_entry}\n", 1
                        )
                        break
            else:
                content += f"\n## Done\n{done_entry}\n"

            # Clean up blank lines
            while "\n\n\n" in content:
                content = content.replace("\n\n\n", "\n\n")

            tasks_path.write_text(content)
            logger.info("Moved %s to TASKS.md Done", task_id)
        except Exception:
            logger.warning("Could not complete %s in TASKS.md", task_id, exc_info=True)

    def _fail_task(self, task_id: str, error: str) -> None:
        """Mark a task as failed in TASKS.md Active section."""
        tasks_path = self._working_dir / "TASKS.md"
        try:
            content = tasks_path.read_text() if tasks_path.exists() else ""
            content = content.replace(
                "  status: worker dispatched\n",
                f"  status: failed -- {error[:200]}\n",
                1,
            )
            tasks_path.write_text(content)
        except Exception:
            logger.warning("Could not mark %s as failed", task_id, exc_info=True)

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

            self._complete_task(task_id, summary)
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
            self._fail_task(task_id, str(e))

            # Notify Director of failure
            self._manager.notify(
                self._instance_name,
                self._director_conversation_id,
                f'[WORKER REPORT] Task "{task_id}" FAILED.\nError: {e}',
            )
