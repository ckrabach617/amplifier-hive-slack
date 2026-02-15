"""Tests for dispatch.py -- DispatchWorkerTool lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hive_slack.dispatch import DispatchWorkerTool
from hive_slack.task_store import SECTION_ACTIVE, SECTION_DONE, parse_tasks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeSessionManager:
    """Minimal mock of InProcessSessionManager for dispatch tests."""

    def __init__(self, response: str = "Worker result text") -> None:
        self.execute = AsyncMock(return_value=response)
        self.notify = MagicMock()


@pytest.fixture
def working_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def manager() -> FakeSessionManager:
    return FakeSessionManager()


@pytest.fixture
def tool(manager: FakeSessionManager, working_dir: Path) -> DispatchWorkerTool:
    return DispatchWorkerTool(
        session_manager=manager,
        instance_name="alpha",
        working_dir=str(working_dir),
        director_conversation_id="test-channel:director",
    )


def read_tasks(working_dir: Path):
    """Helper to read and parse TASKS.md from the working dir."""
    tasks_path = working_dir / "TASKS.md"
    if not tasks_path.exists():
        return parse_tasks("")
    return parse_tasks(tasks_path.read_text())


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------


class TestToolMetadata:
    def test_name(self, tool: DispatchWorkerTool):
        assert tool.name == "dispatch_worker"

    def test_has_required_fields(self, tool: DispatchWorkerTool):
        schema = tool.input_schema
        assert "task" in schema["properties"]
        assert "task_id" in schema["properties"]
        assert set(schema["required"]) == {"task", "task_id"}


# ---------------------------------------------------------------------------
# execute() -- input validation
# ---------------------------------------------------------------------------


class TestExecuteValidation:
    @pytest.mark.asyncio
    async def test_rejects_empty_task(self, tool: DispatchWorkerTool):
        result = await tool.execute({"task": "", "task_id": "test"})
        assert not result.success
        assert "No task" in result.output

    @pytest.mark.asyncio
    async def test_rejects_empty_task_id(self, tool: DispatchWorkerTool):
        result = await tool.execute({"task": "Do something", "task_id": ""})
        assert not result.success
        assert "No task_id" in result.output

    @pytest.mark.asyncio
    async def test_rejects_missing_fields(self, tool: DispatchWorkerTool):
        result = await tool.execute({})
        assert not result.success


# ---------------------------------------------------------------------------
# execute() -- happy path
# ---------------------------------------------------------------------------


class TestExecuteDispatch:
    @pytest.mark.asyncio
    async def test_returns_success_immediately(
        self, tool: DispatchWorkerTool, manager: FakeSessionManager
    ):
        # Make manager.execute hang so we can verify dispatch returns first
        manager.execute = AsyncMock(side_effect=asyncio.sleep(10))

        result = await tool.execute(
            {"task": "Research fire pits", "task_id": "fire-pit"}
        )
        assert result.success
        assert "fire-pit" in result.output

    @pytest.mark.asyncio
    async def test_adds_task_to_active(
        self, tool: DispatchWorkerTool, working_dir: Path
    ):
        await tool.execute({"task": "Research fire pits", "task_id": "fire-pit"})
        tf = read_tasks(working_dir)
        active = tf.get_section(SECTION_ACTIVE)
        assert len(active) == 1
        assert active[0].id == "fire-pit"
        assert active[0].fields["status"] == "worker dispatched"

    @pytest.mark.asyncio
    async def test_increments_worker_counter(self, tool: DispatchWorkerTool):
        assert tool._worker_counter == 0
        await tool.execute({"task": "Task 1", "task_id": "t1"})
        assert tool._worker_counter == 1
        await tool.execute({"task": "Task 2", "task_id": "t2"})
        assert tool._worker_counter == 2


# ---------------------------------------------------------------------------
# _run_worker -- success path
# ---------------------------------------------------------------------------


class TestWorkerSuccess:
    @pytest.mark.asyncio
    async def test_completes_task_in_tasks_md(
        self,
        tool: DispatchWorkerTool,
        manager: FakeSessionManager,
        working_dir: Path,
    ):
        manager.execute = AsyncMock(return_value="Research complete: top 3 options")

        await tool.execute({"task": "Research decks", "task_id": "deck-research"})
        # Wait for background task to finish
        await asyncio.sleep(0.1)

        tf = read_tasks(working_dir)
        # Should have moved from Active to Done
        assert len(tf.get_section(SECTION_ACTIVE)) == 0
        done = tf.get_section(SECTION_DONE)
        assert len(done) == 1
        assert done[0].id == "deck-research"
        assert "Research complete" in done[0].fields["summary"]

    @pytest.mark.asyncio
    async def test_notifies_director_on_success(
        self, tool: DispatchWorkerTool, manager: FakeSessionManager
    ):
        manager.execute = AsyncMock(return_value="All done")

        await tool.execute({"task": "Do thing", "task_id": "my-task"})
        await asyncio.sleep(0.1)

        manager.notify.assert_called_once()
        args = manager.notify.call_args
        assert args[0][0] == "alpha"  # instance_name
        assert args[0][1] == "test-channel:director"  # conversation_id
        assert "my-task" in args[0][2]
        assert "completed" in args[0][2]

    @pytest.mark.asyncio
    async def test_truncates_long_summaries(
        self,
        tool: DispatchWorkerTool,
        manager: FakeSessionManager,
        working_dir: Path,
    ):
        manager.execute = AsyncMock(return_value="x" * 1000)

        await tool.execute({"task": "Big result", "task_id": "big"})
        await asyncio.sleep(0.1)

        tf = read_tasks(working_dir)
        done = tf.get_section(SECTION_DONE)
        assert len(done[0].fields["summary"]) < 600  # 500 + truncation note


# ---------------------------------------------------------------------------
# _run_worker -- failure path
# ---------------------------------------------------------------------------


class TestWorkerFailure:
    @pytest.mark.asyncio
    async def test_marks_task_failed_on_error(
        self,
        tool: DispatchWorkerTool,
        manager: FakeSessionManager,
        working_dir: Path,
    ):
        manager.execute = AsyncMock(side_effect=RuntimeError("LLM exploded"))

        await tool.execute({"task": "Doomed task", "task_id": "doomed"})
        await asyncio.sleep(0.1)

        tf = read_tasks(working_dir)
        active = tf.get_section(SECTION_ACTIVE)
        assert len(active) == 1
        assert active[0].id == "doomed"
        assert "failed" in active[0].fields["status"]
        assert "LLM exploded" in active[0].fields["status"]

    @pytest.mark.asyncio
    async def test_notifies_director_on_failure(
        self, tool: DispatchWorkerTool, manager: FakeSessionManager
    ):
        manager.execute = AsyncMock(side_effect=RuntimeError("boom"))

        await tool.execute({"task": "Fail task", "task_id": "fail-task"})
        await asyncio.sleep(0.1)

        manager.notify.assert_called_once()
        args = manager.notify.call_args
        assert "FAILED" in args[0][2]
        assert "boom" in args[0][2]

    @pytest.mark.asyncio
    async def test_failure_does_not_affect_other_tasks(
        self,
        tool: DispatchWorkerTool,
        manager: FakeSessionManager,
        working_dir: Path,
    ):
        """Regression: old _fail_task used blind .replace() and could
        mark the wrong task as failed."""
        # First dispatch succeeds (hangs until we let it)
        call_count = 0

        async def conditional_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First worker: slow but succeeds
                await asyncio.sleep(0.2)
                return "First worker done"
            else:
                # Second worker: fails fast
                raise RuntimeError("second worker broke")

        manager.execute = AsyncMock(side_effect=conditional_execute)

        await tool.execute({"task": "Slow task", "task_id": "slow-task"})
        await tool.execute({"task": "Fast fail", "task_id": "fast-fail"})

        # Wait for both to complete
        await asyncio.sleep(0.5)

        tf = read_tasks(working_dir)
        # slow-task should be in Done (succeeded)
        done = tf.get_section(SECTION_DONE)
        slow = next((t for t in done if t.id == "slow-task"), None)
        assert slow is not None, "slow-task should have completed successfully"

        # fast-fail should be failed in Active
        active = tf.get_section(SECTION_ACTIVE)
        fast = next((t for t in active if t.id == "fast-fail"), None)
        assert fast is not None, "fast-fail should still be in Active"
        assert "failed" in fast.fields["status"]


# ---------------------------------------------------------------------------
# Multiple dispatches
# ---------------------------------------------------------------------------


class TestMultipleDispatches:
    @pytest.mark.asyncio
    async def test_multiple_workers_complete_independently(
        self,
        tool: DispatchWorkerTool,
        manager: FakeSessionManager,
        working_dir: Path,
    ):
        call_count = 0

        async def sequential_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return f"Result {call_count}"

        manager.execute = AsyncMock(side_effect=sequential_execute)

        await tool.execute({"task": "Task A", "task_id": "task-a"})
        await tool.execute({"task": "Task B", "task_id": "task-b"})
        await asyncio.sleep(0.2)

        tf = read_tasks(working_dir)
        assert len(tf.get_section(SECTION_ACTIVE)) == 0
        done_ids = {t.id for t in tf.get_section(SECTION_DONE)}
        assert done_ids == {"task-a", "task-b"}
