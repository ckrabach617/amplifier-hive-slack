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
from hive_slack.worker_manager import WorkerManager

logger = logging.getLogger(__name__)

PHASE_TIMEOUT = 600  # seconds per verification phase


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
        worker_manager: WorkerManager | None = None,
    ) -> None:
        self._manager = session_manager
        self._instance_name = instance_name
        self._working_dir = Path(working_dir).expanduser()
        self._director_conversation_id = director_conversation_id
        self._worker_counter = 0
        self._store = TaskStore(self._working_dir / "TASKS.md")
        self._workers = worker_manager or WorkerManager()

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
                "tier": {
                    "type": "string",
                    "description": (
                        "Which tier this task was classified as "
                        "(e.g., '2', '2+', '3'). For observability."
                    ),
                },
                "verification": {
                    "type": "boolean",
                    "description": (
                        "Set to true for Tier 2 research tasks to enable "
                        "two-pass verification (researcher + verifier chain)."
                    ),
                },
            },
            "required": ["task", "task_id"],
        }

    def _build_verifier_prompt(self, task_id: str) -> str:
        """Build the verifier worker's prompt with claim-checking instructions."""
        return (
            "First read REMEMBER.md for available tools, existing work products, "
            "and gotchas.\n\n"
            f"Read `.outbox/{task_id}-research.md` -- it contains research findings "
            "with claims and sources.\n\n"
            "For each claim:\n"
            "1. Check whether the cited source actually supports the claim.\n"
            "2. Search for at least one additional source to corroborate or contradict.\n"
            "3. Rate confidence: CONFIRMED, CONFLICTING, or UNVERIFIED.\n\n"
            f"Save your verification to `.outbox/{task_id}-verification.md`"
        )

    def _build_researcher_prompt(self, task: str, task_id: str) -> str:
        """Build the researcher worker's prompt with structured output instructions."""
        return (
            "First read REMEMBER.md for available tools, existing work products, "
            "and gotchas.\n\n"
            f"{task}\n\n"
            f"Save your complete findings to `.outbox/{task_id}-research.md`\n\n"
            "Structure your output with:\n"
            "- A Summary section\n"
            "- Numbered Claims with the source URL/reference for each claim"
        )

    async def _run_verified_worker(self, task: str, task_id: str) -> None:
        """Run two-pass verified research: researcher then verifier."""
        outbox = self._working_dir / ".outbox"
        research_file = outbox / f"{task_id}-research.md"
        verification_file = outbox / f"{task_id}-verification.md"

        try:
            # Phase 1: Research
            logger.info("Verified worker Phase 1 (research): %s", task_id)
            research_conv = f"worker:{task_id}:{self._worker_counter}:research"
            try:
                await asyncio.wait_for(
                    self._manager.execute(
                        self._instance_name,
                        research_conv,
                        self._build_researcher_prompt(task, task_id),
                    ),
                    timeout=PHASE_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                reason = "Research timed out"
                await self._store.fail_task(task_id, reason)
                self._manager.notify(
                    self._instance_name,
                    self._director_conversation_id,
                    f'[WORKER REPORT] Task "{task_id}" FAILED.\nError: {reason}',
                )
                return
            except Exception as e:
                reason = f"Research failed: {e}"
                await self._store.fail_task(task_id, reason)
                self._manager.notify(
                    self._instance_name,
                    self._director_conversation_id,
                    f'[WORKER REPORT] Task "{task_id}" FAILED.\nError: {reason}',
                )
                return

            # Validate research output
            if not research_file.exists() or not research_file.read_text().strip():
                reason = (
                    "Research worker completed but didn't produce structured output."
                )
                await self._store.fail_task(task_id, reason)
                self._manager.notify(
                    self._instance_name,
                    self._director_conversation_id,
                    f'[WORKER REPORT] Task "{task_id}" FAILED.\nError: {reason}',
                )
                return

            research_content = research_file.read_text()

            # Phase 2: Verification
            logger.info("Verified worker Phase 2 (verification): %s", task_id)
            verify_conv = f"worker:{task_id}:{self._worker_counter}:verify"

            try:
                await asyncio.wait_for(
                    self._manager.execute(
                        self._instance_name,
                        verify_conv,
                        self._build_verifier_prompt(task_id),
                    ),
                    timeout=PHASE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("Verified worker verification timed out: %s", task_id)
                await self._store.fail_task(task_id, "Verification timed out")
                self._manager.notify(
                    self._instance_name,
                    self._director_conversation_id,
                    f'[WORKER REPORT] Task "{task_id}" partially complete.\n'
                    "Research completed but verification failed.\n"
                    f"Unverified results in .outbox/{task_id}-research.md",
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Verified worker verification failed: %s", task_id)
                await self._store.fail_task(task_id, f"Verification failed: {e}")
                self._manager.notify(
                    self._instance_name,
                    self._director_conversation_id,
                    f'[WORKER REPORT] Task "{task_id}" partially complete.\n'
                    "Research completed but verification failed.\n"
                    f"Unverified results in .outbox/{task_id}-research.md",
                )
                return

            verification_content = (
                verification_file.read_text() if verification_file.exists() else ""
            )

            # Synthesis
            summary = (
                f"Verified research complete. "
                f"Research: {research_content[:200]} "
                f"Verification: {verification_content[:200]}"
            )
            if len(summary) > 500:
                summary = summary[:500] + "... [truncated]"

            await self._store.complete_task(task_id, summary)
            logger.info("Verified worker completed: %s", task_id)

            self._manager.notify(
                self._instance_name,
                self._director_conversation_id,
                f'[WORKER REPORT] Task "{task_id}" completed with verification.\n'
                f"Research:\n{research_content}\n\n"
                f"Verification:\n{verification_content}",
            )

        finally:
            # Cleanup intermediate files
            for f in (research_file, verification_file):
                if f.exists():
                    f.unlink()

    async def execute(self, input: dict[str, Any]) -> Any:
        """Dispatch a background worker and return immediately."""
        from amplifier_core.models import ToolResult

        task = input.get("task", "")
        task_id = input.get("task_id", "")
        tier = input.get("tier", "unknown")

        if not task:
            return ToolResult(success=False, output="No task provided")
        if not task_id:
            return ToolResult(success=False, output="No task_id provided")

        logger.info(
            "TIER_DISPATCH tier=%s task_id=%s task=%s",
            tier,
            task_id,
            task[:100],
        )

        self._worker_counter += 1

        # Add to TASKS.md as Active immediately
        await self._store.add_active(task_id, task)

        # Launch background task
        worker_task = asyncio.create_task(
            self._run_worker(task, task_id),
            name=f"worker-{task_id}",
        )
        self._workers.register(task_id, worker_task, description=task[:100], tier=tier)

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

        except asyncio.CancelledError:
            logger.warning("Background worker cancelled: %s", task_id)
            await self._store.fail_task(task_id, "cancelled")

            self._manager.notify(
                self._instance_name,
                self._director_conversation_id,
                f'[WORKER REPORT] Task "{task_id}" was cancelled.',
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
