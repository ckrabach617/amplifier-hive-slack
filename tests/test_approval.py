"""Tests for SlackApprovalSystem."""

import asyncio

import pytest
from unittest.mock import AsyncMock


class TestSlackApprovalSystem:

    def test_import(self):
        """Module can be imported."""
        from hive_slack.approval import SlackApprovalSystem  # noqa: F401

    @pytest.mark.asyncio
    async def test_request_approval_posts_blocks(self):
        """request_approval posts a Block Kit message."""
        from hive_slack.approval import SlackApprovalSystem

        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "msg123"}
        client.chat_update = AsyncMock()

        approval = SlackApprovalSystem(client, "C123", "thread123")

        # Resolve immediately in a background task
        async def resolve_soon():
            await asyncio.sleep(0.1)
            for cid, (event, holder) in approval._pending.items():
                holder.append("allow")
                event.set()
                break

        asyncio.create_task(resolve_soon())
        result = await approval.request_approval(
            "Delete files?", ["allow", "deny"], timeout=5.0, default="deny"
        )

        assert result == "allow"
        client.chat_postMessage.assert_called_once()
        call_kwargs = client.chat_postMessage.call_args[1]
        assert "blocks" in call_kwargs
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["thread_ts"] == "thread123"

    @pytest.mark.asyncio
    async def test_timeout_returns_default(self):
        """If no response within timeout, returns default."""
        from hive_slack.approval import SlackApprovalSystem

        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "msg123"}
        client.chat_update = AsyncMock()

        approval = SlackApprovalSystem(client, "C123", "thread123")
        result = await approval.request_approval(
            "Delete?", ["allow", "deny"], timeout=0.1, default="deny"
        )

        assert result == "deny"

    @pytest.mark.asyncio
    async def test_message_updated_after_selection(self):
        """After selection, the approval message is updated to remove buttons."""
        from hive_slack.approval import SlackApprovalSystem

        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "msg123"}
        client.chat_update = AsyncMock()

        approval = SlackApprovalSystem(client, "C123", "thread123")

        async def resolve_soon():
            await asyncio.sleep(0.05)
            for cid, (event, holder) in approval._pending.items():
                holder.append("allow")
                event.set()
                break

        asyncio.create_task(resolve_soon())
        await approval.request_approval(
            "Delete?", ["allow", "deny"], timeout=5.0, default="deny"
        )

        # chat_update should have been called to replace the buttons
        client.chat_update.assert_called_once()
        update_kwargs = client.chat_update.call_args[1]
        assert update_kwargs["ts"] == "msg123"
        assert "allow" in update_kwargs["text"]

    def test_resolve_approval_sets_event(self):
        """resolve_approval resolves a pending approval."""
        from hive_slack.approval import SlackApprovalSystem

        approval = SlackApprovalSystem(AsyncMock(), "C123")

        event = asyncio.Event()
        holder: list[str] = []
        approval._pending["abc123"] = (event, holder)

        resolved = approval.resolve_approval("approval_abc123_allow", "allow")

        assert resolved is True
        assert event.is_set()
        assert holder == ["allow"]

    def test_resolve_unknown_returns_false(self):
        """resolve_approval returns False for unknown correlation IDs."""
        from hive_slack.approval import SlackApprovalSystem

        approval = SlackApprovalSystem(AsyncMock(), "C123")
        assert approval.resolve_approval("approval_unknown_allow", "allow") is False

    def test_resolve_non_approval_returns_false(self):
        """resolve_approval returns False for non-approval actions."""
        from hive_slack.approval import SlackApprovalSystem

        approval = SlackApprovalSystem(AsyncMock(), "C123")
        assert approval.resolve_approval("something_else", "value") is False

    @pytest.mark.asyncio
    async def test_pending_cleaned_up_after_resolution(self):
        """Pending entry is removed after request_approval completes."""
        from hive_slack.approval import SlackApprovalSystem

        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "msg123"}
        client.chat_update = AsyncMock()

        approval = SlackApprovalSystem(client, "C123")

        # Let it timeout quickly
        await approval.request_approval(
            "Delete?", ["allow", "deny"], timeout=0.05, default="deny"
        )

        # Pending should be cleaned up
        assert len(approval._pending) == 0

    @pytest.mark.asyncio
    async def test_blocks_contain_buttons_for_each_option(self):
        """Block Kit blocks have one button per option."""
        from hive_slack.approval import SlackApprovalSystem

        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "msg123"}
        client.chat_update = AsyncMock()

        approval = SlackApprovalSystem(client, "C123")

        # Let it timeout
        await approval.request_approval(
            "Proceed?", ["yes", "no", "maybe"], timeout=0.05, default="no"
        )

        call_kwargs = client.chat_postMessage.call_args[1]
        blocks = call_kwargs["blocks"]
        # Second block is the actions block
        actions_block = blocks[1]
        assert actions_block["type"] == "actions"
        assert len(actions_block["elements"]) == 3
        button_texts = [e["text"]["text"] for e in actions_block["elements"]]
        assert button_texts == ["yes", "no", "maybe"]
