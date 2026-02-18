"""Tests for InProcessSessionManager."""

import asyncio
import json
import time

import pytest
from unittest.mock import AsyncMock, MagicMock

from hive_slack.worker_manager import WorkerInfo

from hive_slack.config import (
    HiveSlackConfig,
    InstanceConfig,
    PersonaConfig,
    SlackConfig,
)
from hive_slack.service import InProcessSessionManager


def make_config(working_dir: str = "/tmp/test-workspace") -> HiveSlackConfig:
    return HiveSlackConfig(
        instances={
            "alpha": InstanceConfig(
                name="alpha",
                bundle="foundation",
                working_dir=working_dir,
                persona=PersonaConfig(name="Alpha", emoji=":robot_face:"),
            ),
        },
        default_instance="alpha",
        slack=SlackConfig(
            app_token="xapp-test",
            bot_token="xoxb-test",
        ),
    )


class TestInProcessSessionManager:
    """Test session management logic with mocked Amplifier internals."""

    @pytest.mark.asyncio
    async def test_execute_before_start_raises(self):
        """Calling execute before start() raises RuntimeError."""
        manager = InProcessSessionManager(make_config())
        with pytest.raises(RuntimeError, match="not started"):
            await manager.execute("alpha", "conv-1", "hello")

    @pytest.mark.asyncio
    async def test_execute_returns_session_response(self):
        """execute() returns the string from session.execute()."""
        manager = InProcessSessionManager(make_config())

        mock_session = AsyncMock()
        mock_session.execute.return_value = "I am a response"
        mock_session.cleanup = AsyncMock()

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)

        manager._prepared = {"foundation": mock_prepared}

        result = await manager.execute("alpha", "conv-1", "hello")
        assert result == "I am a response"

    @pytest.mark.asyncio
    async def test_reuses_session_for_same_conversation(self):
        """Same conversation_id reuses the same session."""
        manager = InProcessSessionManager(make_config())

        mock_session = AsyncMock()
        mock_session.execute.return_value = "response"
        mock_session.cleanup = AsyncMock()

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)

        manager._prepared = {"foundation": mock_prepared}

        await manager.execute("alpha", "conv-1", "first")
        await manager.execute("alpha", "conv-1", "second")

        # create_session called only once (session reused)
        assert mock_prepared.create_session.call_count == 1
        # execute called twice on the same session
        assert mock_session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_creates_separate_sessions_per_conversation(self):
        """Different conversation_ids get different sessions."""
        manager = InProcessSessionManager(make_config())

        mock_session_a = AsyncMock()
        mock_session_a.execute.return_value = "response-a"
        mock_session_a.cleanup = AsyncMock()

        mock_session_b = AsyncMock()
        mock_session_b.execute.return_value = "response-b"
        mock_session_b.cleanup = AsyncMock()

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(
            side_effect=[mock_session_a, mock_session_b]
        )

        manager._prepared = {"foundation": mock_prepared}

        result_a = await manager.execute("alpha", "conv-A", "hello A")
        result_b = await manager.execute("alpha", "conv-B", "hello B")

        assert result_a == "response-a"
        assert result_b == "response-b"
        assert mock_prepared.create_session.call_count == 2

    @pytest.mark.asyncio
    async def test_stop_cleans_up_all_sessions(self):
        """stop() calls cleanup on all sessions and clears state."""
        manager = InProcessSessionManager(make_config())

        mock_session = AsyncMock()
        mock_session.execute.return_value = "response"
        mock_session.cleanup = AsyncMock()

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)

        manager._prepared = {"foundation": mock_prepared}

        await manager.execute("alpha", "conv-1", "hello")
        await manager.stop()

        mock_session.cleanup.assert_called_once()
        assert len(manager._sessions) == 0
        assert len(manager._locks) == 0

    @pytest.mark.asyncio
    async def test_concurrent_execute_serializes_per_session(self):
        """Concurrent calls to the same conversation_id are serialized."""
        manager = InProcessSessionManager(make_config())

        execution_order = []

        async def slow_execute(prompt):
            execution_order.append(f"start:{prompt}")
            await asyncio.sleep(0.1)
            execution_order.append(f"end:{prompt}")
            return f"response to {prompt}"

        mock_session = AsyncMock()
        mock_session.execute = slow_execute
        mock_session.cleanup = AsyncMock()

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)

        manager._prepared = {"foundation": mock_prepared}

        # Fire two concurrent executions for the same conversation
        await asyncio.gather(
            manager.execute("alpha", "conv-1", "first"),
            manager.execute("alpha", "conv-1", "second"),
        )

        # Should be serialized: start-first, end-first, start-second, end-second
        assert execution_order[0] == "start:first"
        assert execution_order[1] == "end:first"
        assert execution_order[2] == "start:second"
        assert execution_order[3] == "end:second"

    @pytest.mark.asyncio
    async def test_routes_to_correct_instance_bundle(self):
        """Different instances use their own working directories."""
        config = HiveSlackConfig(
            instances={
                "alpha": InstanceConfig(
                    name="alpha",
                    bundle="foundation",
                    working_dir="/tmp/alpha",
                    persona=PersonaConfig(name="Alpha", emoji=":robot_face:"),
                ),
                "beta": InstanceConfig(
                    name="beta",
                    bundle="foundation",
                    working_dir="/tmp/beta",
                    persona=PersonaConfig(name="Beta", emoji=":gear:"),
                ),
            },
            default_instance="alpha",
            slack=SlackConfig(app_token="xapp-test", bot_token="xoxb-test"),
        )
        manager = InProcessSessionManager(config)

        mock_session_alpha = AsyncMock()
        mock_session_alpha.execute.return_value = "alpha response"
        mock_session_alpha.cleanup = AsyncMock()

        mock_session_beta = AsyncMock()
        mock_session_beta.execute.return_value = "beta response"
        mock_session_beta.cleanup = AsyncMock()

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(
            side_effect=[mock_session_alpha, mock_session_beta]
        )
        manager._prepared = {"foundation": mock_prepared}

        result_a = await manager.execute("alpha", "conv-1", "hello from alpha")
        result_b = await manager.execute("beta", "conv-1", "hello from beta")

        assert result_a == "alpha response"
        assert result_b == "beta response"
        # Two separate sessions created (different instance names, same conv_id)
        assert mock_prepared.create_session.call_count == 2


class TestSessionPersistence:
    """Test transcript persistence after execution."""

    @pytest.mark.asyncio
    async def test_save_transcript_creates_files(self, tmp_path, monkeypatch):
        """Transcript and metadata files are created after execution."""
        monkeypatch.setattr("hive_slack.service.SESSIONS_DIR", tmp_path)

        manager = InProcessSessionManager(make_config())

        mock_context = MagicMock()
        mock_context.get_messages.return_value = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        mock_session = AsyncMock()
        mock_session.execute.return_value = "hi there"
        mock_session.cleanup = AsyncMock()
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.get.return_value = mock_context

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        manager._prepared = {"foundation": mock_prepared}

        await manager.execute("alpha", "C123:thread1", "hello")

        # Check transcript file exists
        transcript_dir = tmp_path / "alpha" / "C123_thread1"
        assert (transcript_dir / "transcript.jsonl").exists()
        assert (transcript_dir / "metadata.json").exists()

        # Check transcript content
        lines = (transcript_dir / "transcript.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["role"] == "user"
        assert json.loads(lines[1])["role"] == "assistant"

        # Check metadata content
        metadata = json.loads((transcript_dir / "metadata.json").read_text())
        assert metadata["instance"] == "alpha"
        assert metadata["conversation_id"] == "C123:thread1"
        assert metadata["turn_count"] == 1

    @pytest.mark.asyncio
    async def test_save_transcript_handles_missing_context(self, tmp_path, monkeypatch):
        """Persistence gracefully handles sessions without get_messages."""
        monkeypatch.setattr("hive_slack.service.SESSIONS_DIR", tmp_path)

        manager = InProcessSessionManager(make_config())

        mock_session = AsyncMock()
        mock_session.execute.return_value = "hi there"
        mock_session.cleanup = AsyncMock()
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.get.return_value = None  # No context manager

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        manager._prepared = {"foundation": mock_prepared}

        # Should not raise, just log a warning
        result = await manager.execute("alpha", "C123:thread1", "hello")
        assert result == "hi there"

    @pytest.mark.asyncio
    async def test_save_transcript_does_not_break_execute(self, tmp_path, monkeypatch):
        """If persistence fails, execute() still returns the response."""
        # Point to a read-only directory to force an error
        read_only_dir = tmp_path / "readonly"
        read_only_dir.mkdir()
        read_only_dir.chmod(0o444)
        monkeypatch.setattr("hive_slack.service.SESSIONS_DIR", read_only_dir)

        manager = InProcessSessionManager(make_config())

        mock_context = MagicMock()
        mock_context.get_messages.return_value = [
            {"role": "user", "content": "hello"},
        ]

        mock_session = AsyncMock()
        mock_session.execute.return_value = "response works"
        mock_session.cleanup = AsyncMock()
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.get.return_value = mock_context

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        manager._prepared = {"foundation": mock_prepared}

        # Should still return the response even if persistence fails
        result = await manager.execute("alpha", "C123:thread1", "hello")
        assert result == "response works"

        # Restore permissions for cleanup
        read_only_dir.chmod(0o755)


class TestOnProgressCallback:
    """Test on_progress callback support in execute()."""

    @pytest.mark.asyncio
    async def test_execute_calls_on_progress(self):
        """on_progress callback is called with executing and complete events."""
        manager = InProcessSessionManager(make_config())

        mock_session = AsyncMock()
        mock_session.execute.return_value = "response"
        mock_session.cleanup = AsyncMock()
        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        manager._prepared = {"foundation": mock_prepared}

        progress_events = []

        async def on_progress(event_type, data):
            progress_events.append((event_type, data))

        await manager.execute("alpha", "conv-1", "hello", on_progress=on_progress)

        assert len(progress_events) == 2
        assert progress_events[0][0] == "executing"
        assert progress_events[1][0] == "complete"

    @pytest.mark.asyncio
    async def test_execute_without_on_progress_still_works(self):
        """Existing calls without on_progress continue to work."""
        manager = InProcessSessionManager(make_config())

        mock_session = AsyncMock()
        mock_session.execute.return_value = "response"
        mock_session.cleanup = AsyncMock()
        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        manager._prepared = {"foundation": mock_prepared}

        result = await manager.execute("alpha", "conv-1", "hello")
        assert result == "response"

    @pytest.mark.asyncio
    async def test_on_progress_receives_error_on_failure(self):
        """on_progress receives error event when execution fails."""
        manager = InProcessSessionManager(make_config())

        mock_session = AsyncMock()
        mock_session.execute.side_effect = RuntimeError("boom")
        mock_session.cleanup = AsyncMock()
        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        manager._prepared = {"foundation": mock_prepared}

        progress_events = []

        async def on_progress(event_type, data):
            progress_events.append((event_type, data))

        with pytest.raises(RuntimeError, match="boom"):
            await manager.execute("alpha", "conv-1", "hello", on_progress=on_progress)

        assert progress_events[0][0] == "executing"
        assert progress_events[1][0] == "error"

    @pytest.mark.asyncio
    async def test_on_progress_callback_error_does_not_break_execute(self):
        """If on_progress callback raises, execute() still works."""
        manager = InProcessSessionManager(make_config())

        mock_session = AsyncMock()
        mock_session.execute.return_value = "response"
        mock_session.cleanup = AsyncMock()
        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        manager._prepared = {"foundation": mock_prepared}

        async def bad_callback(event_type, data):
            raise ValueError("callback crashed")

        # Should not raise despite callback error
        result = await manager.execute(
            "alpha", "conv-1", "hello", on_progress=bad_callback
        )
        assert result == "response"


class TestGetStatus:
    """Test status collection from InProcessSessionManager."""

    def test_returns_all_expected_keys(self):
        """get_status() returns a dict with the full set of status keys."""
        manager = InProcessSessionManager(make_config())
        manager._started_at = time.monotonic() - 100

        status = manager.get_status()

        assert "uptime_seconds" in status
        assert "recipes_available" in status
        assert "workers" in status
        assert "sessions_count" in status
        assert "executing_count" in status
        assert "queued_message_count" in status
        assert "connection" in status

    def test_uptime_reflects_elapsed_time(self):
        """uptime_seconds reflects time since _started_at was set."""
        manager = InProcessSessionManager(make_config())
        manager._started_at = time.monotonic() - 3600

        status = manager.get_status()

        assert 3599 <= status["uptime_seconds"] <= 3601

    def test_uptime_none_when_not_started(self):
        """uptime_seconds is None when service hasn't started."""
        manager = InProcessSessionManager(make_config())

        status = manager.get_status()

        assert status["uptime_seconds"] is None

    def test_recipes_available_flag(self):
        """recipes_available reflects the internal flag."""
        manager = InProcessSessionManager(make_config())
        manager._started_at = time.monotonic()
        manager._recipes_available = False

        status = manager.get_status()

        assert status["recipes_available"] is False

    def test_sessions_and_executing_counts(self):
        """sessions_count and executing_count reflect internal state."""
        manager = InProcessSessionManager(make_config())
        manager._started_at = time.monotonic()
        manager._sessions = {"a:1": MagicMock(), "b:2": MagicMock(), "c:3": MagicMock()}
        manager._executing = {"a:1"}

        status = manager.get_status()

        assert status["sessions_count"] == 3
        assert status["executing_count"] == 1

    def test_queued_message_count_passed_through(self):
        """queued_message_count is passed through from the caller."""
        manager = InProcessSessionManager(make_config())
        manager._started_at = time.monotonic()

        status = manager.get_status(queued_message_count=5)

        assert status["queued_message_count"] == 5

    def test_active_workers_included(self):
        """Workers section includes task_id, tier, and elapsed time."""
        manager = InProcessSessionManager(make_config())
        manager._started_at = time.monotonic()

        mock_task = MagicMock()
        mock_task.done.return_value = False
        manager._worker_manager._workers = {
            "test-task": WorkerInfo(
                task_id="test-task",
                description="Test work",
                task=mock_task,
                started_at=time.monotonic() - 120,
                tier="2",
            )
        }

        status = manager.get_status()

        assert len(status["workers"]) == 1
        w = status["workers"][0]
        assert w["task_id"] == "test-task"
        assert w["description"] == "Test work"
        assert w["tier"] == "2"
        assert 119 <= w["elapsed_seconds"] <= 121

    def test_connection_health_healthy(self):
        """Connection shows healthy when recent health check data provided."""
        manager = InProcessSessionManager(make_config())
        manager._started_at = time.monotonic()

        now = time.monotonic()
        connection_health = {
            "started_at": now - 3600,
            "last_health_check_at": now - 45,
            "reconnect_count": 0,
        }

        status = manager.get_status(connection_health=connection_health)

        assert status["connection"]["status"] == "healthy"
        assert 44 <= status["connection"]["seconds_since_last_check"] <= 46
        assert status["connection"]["reconnect_count"] == 0

    def test_connection_health_starting(self):
        """Connection shows starting when started but no health check yet."""
        manager = InProcessSessionManager(make_config())
        manager._started_at = time.monotonic()

        connection_health = {
            "started_at": time.monotonic() - 10,
            "last_health_check_at": None,
            "reconnect_count": 0,
        }

        status = manager.get_status(connection_health=connection_health)

        assert status["connection"]["status"] == "starting"

    def test_connection_health_unknown_when_no_data(self):
        """Connection status is unknown when no health data provided."""
        manager = InProcessSessionManager(make_config())
        manager._started_at = time.monotonic()

        status = manager.get_status()

        assert status["connection"]["status"] == "unknown"

    def test_graceful_degradation_worker_error(self):
        """get_status() returns empty workers if WorkerManager throws."""
        manager = InProcessSessionManager(make_config())
        manager._started_at = time.monotonic()
        manager._worker_manager.get_active = MagicMock(
            side_effect=RuntimeError("broken")
        )

        status = manager.get_status()

        assert status["workers"] == []
        assert "uptime_seconds" in status

    def test_graceful_degradation_connection_error(self):
        """get_status() shows unavailable connection if health data throws."""
        manager = InProcessSessionManager(make_config())
        manager._started_at = time.monotonic()

        bad_health: dict = MagicMock()
        bad_health.get = MagicMock(side_effect=RuntimeError("busted"))

        status = manager.get_status(connection_health=bad_health)

        assert status["connection"]["status"] == "unavailable"
