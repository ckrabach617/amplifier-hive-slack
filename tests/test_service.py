"""Tests for InProcessSessionManager."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from hive_slack.config import HiveSlackConfig, InstanceConfig, PersonaConfig, SlackConfig
from hive_slack.service import InProcessSessionManager


def make_config(working_dir: str = "/tmp/test-workspace") -> HiveSlackConfig:
    return HiveSlackConfig(
        instance=InstanceConfig(
            name="alpha",
            bundle="foundation",
            working_dir=working_dir,
            persona=PersonaConfig(name="Alpha", emoji=":robot_face:"),
        ),
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

        manager._prepared = mock_prepared

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

        manager._prepared = mock_prepared

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

        manager._prepared = mock_prepared

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

        manager._prepared = mock_prepared

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

        manager._prepared = mock_prepared

        # Fire two concurrent executions for the same conversation
        results = await asyncio.gather(
            manager.execute("alpha", "conv-1", "first"),
            manager.execute("alpha", "conv-1", "second"),
        )

        # Should be serialized: start-first, end-first, start-second, end-second
        assert execution_order[0] == "start:first"
        assert execution_order[1] == "end:first"
        assert execution_order[2] == "start:second"
        assert execution_order[3] == "end:second"
