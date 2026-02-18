"""Worker lifecycle manager for background task tracking.

Provides observable worker state: what's running, what finished, what
failed, and what timed out. Replaces ad-hoc fire-and-forget patterns
with a centralized registry that supports cancellation and shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class WorkerInfo:
    """Metadata for a tracked worker task."""

    task_id: str
    description: str
    task: asyncio.Task
    started_at: float = field(default_factory=time.monotonic)


class WorkerManager:
    """Tracks active worker tasks with timeout and cancellation support.

    Usage:
        manager = WorkerManager(timeout=600)  # 10 min default
        manager.register("TASK-007", task, "Research fire pit options")
        ...
        active = manager.get_active()
        manager.cancel("TASK-007")
        await manager.cancel_all()  # on shutdown
    """

    def __init__(self, timeout: float = 600.0) -> None:
        self._workers: dict[str, WorkerInfo] = {}
        self._timeout = timeout

    def register(self, task_id: str, task: asyncio.Task, description: str = "") -> None:
        """Register a new worker task for tracking."""
        if task_id in self._workers:
            logger.warning("Worker %s already registered, replacing", task_id)
        self._workers[task_id] = WorkerInfo(
            task_id=task_id, description=description, task=task
        )
        task.add_done_callback(lambda _t, tid=task_id: self._on_done(tid))

    def unregister(self, task_id: str) -> None:
        """Remove a worker from tracking."""
        self._workers.pop(task_id, None)

    def get_active(self) -> list[WorkerInfo]:
        """Get all currently active workers."""
        return [w for w in self._workers.values() if not w.task.done()]

    def get_all(self) -> list[WorkerInfo]:
        """Get all tracked workers (active and recently completed)."""
        return list(self._workers.values())

    def cancel(self, task_id: str) -> bool:
        """Cancel a worker by task_id. Returns True if cancelled."""
        info = self._workers.get(task_id)
        if info is None or info.task.done():
            return False
        info.task.cancel()
        logger.info("Cancelled worker %s", task_id)
        return True

    async def cancel_all(self) -> None:
        """Cancel all active workers and wait for them to finish.

        Used during graceful shutdown to ensure no orphaned tasks.
        """
        active = self.get_active()
        if not active:
            return

        logger.info("Cancelling %d active worker(s)...", len(active))
        for info in active:
            info.task.cancel()

        # Wait for all tasks to finish (cancelled or otherwise)
        tasks = [info.task for info in active]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("All workers stopped")

    async def run_timeout_watchdog(self, interval: float = 30.0) -> None:
        """Periodically cancel workers that exceed the timeout.

        Runs in a loop. Workers that exceed ``self._timeout`` seconds
        are cancelled automatically.
        """
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            for info in self.get_active():
                elapsed = now - info.started_at
                if elapsed > self._timeout:
                    logger.warning(
                        "Worker %s timed out after %.0fs (limit: %.0fs), cancelling",
                        info.task_id,
                        elapsed,
                        self._timeout,
                    )
                    info.task.cancel()

    def _on_done(self, task_id: str) -> None:
        """Done callback -- log completion and clean up."""
        info = self._workers.pop(task_id, None)
        if info is None:
            return

        if info.task.cancelled():
            logger.info("Worker %s was cancelled", task_id)
        elif info.task.exception():
            exc = info.task.exception()
            logger.error(
                "Worker %s raised unhandled exception: %s",
                task_id,
                exc,
                exc_info=exc,
            )
        else:
            elapsed = time.monotonic() - info.started_at
            logger.info("Worker %s completed in %.1fs", task_id, elapsed)
