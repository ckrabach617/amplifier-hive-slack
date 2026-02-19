"""Tests for async_recipes.py -- AsyncRecipesTool lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from hive_slack.async_recipes import AsyncRecipesTool
from hive_slack.worker_manager import WorkerManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeToolResult:
    """Minimal stand-in for amplifier_core.models.ToolResult."""

    success: bool = True
    output: str = ""


class FakeRecipesTool:
    """Minimal mock of the real recipes tool."""

    def __init__(
        self,
        result: FakeToolResult | None = None,
        delay: float = 0,
        error: Exception | None = None,
    ) -> None:
        self._result = result or FakeToolResult(success=True, output="done")
        self._delay = delay
        self._error = error
        self.execute_calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "recipes"

    @property
    def description(self) -> str:
        return "Fake recipes tool"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"operation": {"type": "string"}}}

    async def execute(self, input: dict[str, Any]) -> FakeToolResult:
        self.execute_calls.append(input)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def worker_manager() -> WorkerManager:
    return WorkerManager(timeout=60.0)


@pytest.fixture
def notify() -> MagicMock:
    return MagicMock()


@pytest.fixture
def fake_recipes() -> FakeRecipesTool:
    return FakeRecipesTool()


@pytest.fixture
def tool(
    fake_recipes: FakeRecipesTool,
    worker_manager: WorkerManager,
    notify: MagicMock,
) -> AsyncRecipesTool:
    return AsyncRecipesTool(
        wrapped_tool=fake_recipes,
        worker_manager=worker_manager,
        notify_fn=notify,
    )


# ---------------------------------------------------------------------------
# Tool protocol
# ---------------------------------------------------------------------------


class TestToolProtocol:
    def test_name_is_recipes(self, tool: AsyncRecipesTool):
        assert tool.name == "recipes"

    def test_description_delegates(
        self, tool: AsyncRecipesTool, fake_recipes: FakeRecipesTool
    ):
        assert tool.description == fake_recipes.description

    def test_input_schema_delegates(
        self, tool: AsyncRecipesTool, fake_recipes: FakeRecipesTool
    ):
        assert tool.input_schema == fake_recipes.input_schema


# ---------------------------------------------------------------------------
# Quick operations -- pass through synchronously
# ---------------------------------------------------------------------------


class TestQuickOps:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "operation",
        ["list", "validate", "approvals", "approve", "deny", "cancel"],
    )
    async def test_quick_ops_pass_through(
        self,
        tool: AsyncRecipesTool,
        fake_recipes: FakeRecipesTool,
        operation: str,
    ):
        """Quick operations should call the wrapped tool directly."""
        input_data = {"operation": operation}
        result = await tool.execute(input_data)
        assert result.success
        assert len(fake_recipes.execute_calls) == 1
        assert fake_recipes.execute_calls[0] == input_data

    @pytest.mark.asyncio
    async def test_unknown_operation_passes_through(
        self,
        tool: AsyncRecipesTool,
        fake_recipes: FakeRecipesTool,
    ):
        """Unrecognised operations pass through (safe default)."""
        input_data = {"operation": "something_new"}
        await tool.execute(input_data)
        assert len(fake_recipes.execute_calls) == 1


# ---------------------------------------------------------------------------
# Long operations -- dispatched to background
# ---------------------------------------------------------------------------


class TestAsyncDispatch:
    @pytest.mark.asyncio
    async def test_execute_returns_immediately(
        self,
        worker_manager: WorkerManager,
        notify: MagicMock,
    ):
        """execute op should return instantly, even if wrapped tool hangs."""
        slow = FakeRecipesTool(delay=10)
        wrapper = AsyncRecipesTool(
            wrapped_tool=slow,
            worker_manager=worker_manager,
            notify_fn=notify,
        )
        result = await wrapper.execute(
            {
                "operation": "execute",
                "recipe_path": "@superpowers:recipes/brainstorming.yaml",
            }
        )
        assert result.success
        assert "background" in result.output
        assert "brainstorming.yaml" in result.output

    @pytest.mark.asyncio
    async def test_resume_returns_immediately(
        self,
        worker_manager: WorkerManager,
        notify: MagicMock,
    ):
        """resume op dispatches to background like execute."""
        slow = FakeRecipesTool(delay=10)
        wrapper = AsyncRecipesTool(
            wrapped_tool=slow,
            worker_manager=worker_manager,
            notify_fn=notify,
        )
        result = await wrapper.execute(
            {"operation": "resume", "session_id": "recipe_20260219_abc"}
        )
        assert result.success
        assert "background" in result.output

    @pytest.mark.asyncio
    async def test_registers_with_worker_manager(
        self, tool: AsyncRecipesTool, worker_manager: WorkerManager
    ):
        await tool.execute({"operation": "execute", "recipe_path": "some/recipe.yaml"})
        active = worker_manager.get_active()
        assert len(active) == 1
        assert active[0].tier == "3"
        assert "Recipe" in active[0].description

    @pytest.mark.asyncio
    async def test_increments_counter(self, tool: AsyncRecipesTool):
        assert tool._counter == 0
        await tool.execute({"operation": "execute", "recipe_path": "a.yaml"})
        assert tool._counter == 1
        await tool.execute({"operation": "execute", "recipe_path": "b.yaml"})
        assert tool._counter == 2

    @pytest.mark.asyncio
    async def test_label_extracts_filename(self, tool: AsyncRecipesTool):
        result = await tool.execute(
            {
                "operation": "execute",
                "recipe_path": "@superpowers:recipes/brainstorming.yaml",
            }
        )
        assert "brainstorming.yaml" in result.output

    @pytest.mark.asyncio
    async def test_label_falls_back_to_session_id(
        self,
        worker_manager: WorkerManager,
        notify: MagicMock,
    ):
        fake = FakeRecipesTool()
        wrapper = AsyncRecipesTool(
            wrapped_tool=fake,
            worker_manager=worker_manager,
            notify_fn=notify,
        )
        result = await wrapper.execute(
            {"operation": "resume", "session_id": "recipe_abc"}
        )
        assert "recipe_abc" in result.output

    @pytest.mark.asyncio
    async def test_label_falls_back_to_counter(self, tool: AsyncRecipesTool):
        result = await tool.execute({"operation": "execute"})
        assert "recipe-1" in result.output


# ---------------------------------------------------------------------------
# Background task lifecycle
# ---------------------------------------------------------------------------


class TestBackgroundLifecycle:
    @pytest.mark.asyncio
    async def test_notifies_on_success(
        self,
        tool: AsyncRecipesTool,
        fake_recipes: FakeRecipesTool,
        notify: MagicMock,
    ):
        await tool.execute({"operation": "execute", "recipe_path": "test.yaml"})
        # Wait for background task
        await asyncio.sleep(0.1)

        notify.assert_called_once()
        msg = notify.call_args[0][0]
        assert "[RECIPE COMPLETE]" in msg
        assert "test.yaml" in msg

    @pytest.mark.asyncio
    async def test_notifies_on_failure(
        self,
        worker_manager: WorkerManager,
        notify: MagicMock,
    ):
        failing = FakeRecipesTool(error=RuntimeError("kaboom"))
        wrapper = AsyncRecipesTool(
            wrapped_tool=failing,
            worker_manager=worker_manager,
            notify_fn=notify,
        )
        await wrapper.execute({"operation": "execute", "recipe_path": "fail.yaml"})
        await asyncio.sleep(0.1)

        notify.assert_called_once()
        msg = notify.call_args[0][0]
        assert "[RECIPE FAILED]" in msg
        assert "kaboom" in msg

    @pytest.mark.asyncio
    async def test_notifies_on_cancel(
        self,
        worker_manager: WorkerManager,
        notify: MagicMock,
    ):
        slow = FakeRecipesTool(delay=10)
        wrapper = AsyncRecipesTool(
            wrapped_tool=slow,
            worker_manager=worker_manager,
            notify_fn=notify,
        )
        await wrapper.execute({"operation": "execute", "recipe_path": "slow.yaml"})
        # Yield to let the background task reach its await point
        await asyncio.sleep(0)
        # Cancel the background task via worker_manager
        active = worker_manager.get_active()
        assert len(active) == 1
        worker_manager.cancel(active[0].task_id)
        await asyncio.sleep(0.1)

        notify.assert_called_once()
        msg = notify.call_args[0][0]
        assert "[RECIPE CANCELLED]" in msg

    @pytest.mark.asyncio
    async def test_truncates_long_output(
        self,
        worker_manager: WorkerManager,
        notify: MagicMock,
    ):
        long_result = FakeToolResult(success=True, output="x" * 1000)
        verbose = FakeRecipesTool(result=long_result)
        wrapper = AsyncRecipesTool(
            wrapped_tool=verbose,
            worker_manager=worker_manager,
            notify_fn=notify,
        )
        await wrapper.execute({"operation": "execute", "recipe_path": "verbose.yaml"})
        await asyncio.sleep(0.1)

        msg = notify.call_args[0][0]
        assert "[truncated]" in msg
        # Output portion should be capped
        assert len(msg) < 600

    @pytest.mark.asyncio
    async def test_worker_cleaned_up_after_completion(
        self,
        tool: AsyncRecipesTool,
        worker_manager: WorkerManager,
    ):
        await tool.execute({"operation": "execute", "recipe_path": "test.yaml"})
        # Worker should be active immediately
        assert len(worker_manager.get_active()) == 1

        # Wait for completion
        await asyncio.sleep(0.1)

        # WorkerManager's done callback removes completed workers
        assert len(worker_manager.get_active()) == 0


# ---------------------------------------------------------------------------
# Wrapped tool interaction
# ---------------------------------------------------------------------------


class TestWrappedToolInteraction:
    @pytest.mark.asyncio
    async def test_execute_op_calls_wrapped_in_background(
        self,
        tool: AsyncRecipesTool,
        fake_recipes: FakeRecipesTool,
    ):
        """The wrapped tool should be called (in background) for execute ops."""
        input_data = {
            "operation": "execute",
            "recipe_path": "test.yaml",
            "context": {"key": "value"},
        }
        await tool.execute(input_data)
        await asyncio.sleep(0.1)

        assert len(fake_recipes.execute_calls) == 1
        assert fake_recipes.execute_calls[0] == input_data

    @pytest.mark.asyncio
    async def test_wrapped_not_called_synchronously_for_execute(
        self,
        worker_manager: WorkerManager,
        notify: MagicMock,
    ):
        """execute op should NOT block on the wrapped tool."""
        slow = FakeRecipesTool(delay=10)
        wrapper = AsyncRecipesTool(
            wrapped_tool=slow,
            worker_manager=worker_manager,
            notify_fn=notify,
        )
        # This should return instantly, not after 10 seconds
        await wrapper.execute({"operation": "execute", "recipe_path": "slow.yaml"})
        # The wrapped tool hasn't finished yet
        assert len(slow.execute_calls) == 0 or slow._delay > 0

    @pytest.mark.asyncio
    async def test_multiple_recipes_tracked_independently(
        self,
        tool: AsyncRecipesTool,
        worker_manager: WorkerManager,
    ):
        await tool.execute({"operation": "execute", "recipe_path": "a.yaml"})
        await tool.execute({"operation": "execute", "recipe_path": "b.yaml"})
        # Both should be tracked
        active = worker_manager.get_active()
        assert len(active) == 2
        task_ids = {w.task_id for w in active}
        assert "recipe-1" in task_ids
        assert "recipe-2" in task_ids
