# Rich Adaptive Status Messages Implementation Plan

> **Execution:** Use the subagent-driven-development workflow to implement this plan.

**Goal:** Upgrade the Slack status message from a simple "⚙️ Working..." line to an adaptive two-mode display that shows the agent's todo list and tool activity in real-time.

**Architecture:** Two pure functions (`_format_duration`, `_render_todo_status`) handle rendering. The `on_tool_pre`/`on_tool_post` hooks in `service.py` are enhanced to extract todo items and delegate agent names from the hook `data` dict. The `on_progress` closure in `slack.py` gains local state (`_status_todos`, `_status_tool`, `_status_agent`) and a 2-second throttle. Mode is determined by whether `_status_todos` is `None` (simple) or a list (plan view). Transition is one-way: simple → plan view when first todo payload arrives.

**Tech Stack:** Python 3.12, pytest (asyncio_mode=auto), Slack Web API (`chat_update` with mrkdwn)

---

## Task 1: Add `_format_duration()` helper to `slack.py`

**Files:**
- Modify: `src/hive_slack/slack.py` (insert after line 221, after `_friendly_tool_name`)
- Test: `tests/test_slack.py` (append new test class)

**Step 1: Write failing tests**

Append the following test class to the end of `tests/test_slack.py`:

```python
class TestFormatDuration:
    """Test duration formatting for status messages."""

    def test_under_10_seconds_returns_empty(self):
        from hive_slack.slack import _format_duration

        assert _format_duration(0.0) == ""
        assert _format_duration(5.0) == ""
        assert _format_duration(9.9) == ""

    def test_seconds_only(self):
        from hive_slack.slack import _format_duration

        assert _format_duration(10.0) == "10s"
        assert _format_duration(30.0) == "30s"
        assert _format_duration(59.0) == "59s"

    def test_minutes_and_seconds(self):
        from hive_slack.slack import _format_duration

        assert _format_duration(90.0) == "1m 30s"
        assert _format_duration(125.0) == "2m 5s"

    def test_exact_minutes(self):
        from hive_slack.slack import _format_duration

        assert _format_duration(60.0) == "1m"
        assert _format_duration(120.0) == "2m"
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/test_slack.py::TestFormatDuration -v`

Expected: FAIL — `ImportError: cannot import name '_format_duration' from 'hive_slack.slack'`

**Step 3: Implement `_format_duration`**

In `src/hive_slack/slack.py`, insert the following function immediately after `_friendly_tool_name` (after line 221, before `class SlackConnector:` on line 224):

```python


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration.

    Returns empty string for durations under 10 seconds (too short to display).
    """
    s = int(seconds)
    if s < 10:
        return ""
    if s < 60:
        return f"{s}s"
    m, rem = divmod(s, 60)
    return f"{m}m {rem}s" if rem else f"{m}m"
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/test_slack.py::TestFormatDuration -v`

Expected: 4 PASSED

**Step 5: Run full test suite to verify no regressions**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/ -v`

Expected: All 199 existing tests + 4 new = 203 PASSED

**Step 6: Commit**

```
feat: add _format_duration() helper for status message timing
```

---

## Task 2: Add `_render_todo_status()` helper to `slack.py`

**Files:**
- Modify: `src/hive_slack/slack.py` (insert after `_format_duration`, before `class SlackConnector:`)
- Test: `tests/test_slack.py` (append new test class)

**Step 1: Write failing tests**

Append the following test class to the end of `tests/test_slack.py` (after `TestFormatDuration`):

```python
class TestRenderTodoStatus:
    """Test plan-mode status message rendering."""

    def test_basic_rendering_with_all_states(self):
        from hive_slack.slack import _render_todo_status

        todos = [
            {"content": "Read files", "status": "completed", "activeForm": "Reading files"},
            {"content": "Analyze code", "status": "in_progress", "activeForm": "Analyzing code"},
            {"content": "Write report", "status": "pending", "activeForm": "Writing report"},
        ]
        result = _render_todo_status(todos, "read_file", "Alpha", "45s", 0)
        assert "Alpha" in result
        assert "45s" in result
        assert "✅" in result
        assert "Read files" in result
        assert "▸" in result
        assert "*Analyzing code*" in result  # activeForm, bolded
        assert "○" in result
        assert "Write report" in result
        assert "1 of 3" in result
        assert "Reading files" in result  # friendly tool name in footer

    def test_collapses_many_completed_items(self):
        from hive_slack.slack import _render_todo_status

        todos = [
            {"content": f"Task {i}", "status": "completed", "activeForm": f"Task {i}"}
            for i in range(5)
        ] + [
            {"content": "Current", "status": "in_progress", "activeForm": "Working"},
        ]
        result = _render_todo_status(todos, "bash", "Alpha", "1m", 0)
        assert "5 completed" in result
        # Individual completed task names should NOT appear
        assert "Task 0" not in result
        assert "▸" in result

    def test_collapses_many_pending_items(self):
        from hive_slack.slack import _render_todo_status

        todos = [
            {"content": "Done", "status": "completed", "activeForm": "Done"},
            {"content": "Current", "status": "in_progress", "activeForm": "Working"},
        ] + [
            {"content": f"Pending {i}", "status": "pending", "activeForm": f"Pending {i}"}
            for i in range(5)
        ]
        result = _render_todo_status(todos, "bash", "Alpha", "", 0)
        # First 2 pending shown
        assert "Pending 0" in result
        assert "Pending 1" in result
        # Rest collapsed
        assert "+3 more" in result
        # Items beyond first 2 should NOT appear
        assert "Pending 4" not in result

    def test_shows_queued_message_count(self):
        from hive_slack.slack import _render_todo_status

        todos = [
            {"content": "Task", "status": "in_progress", "activeForm": "Working"},
        ]
        result = _render_todo_status(todos, "bash", "Alpha", "", 2)
        assert "2 messages queued" in result

    def test_singular_queued_message(self):
        from hive_slack.slack import _render_todo_status

        todos = [
            {"content": "Task", "status": "in_progress", "activeForm": "Working"},
        ]
        result = _render_todo_status(todos, "bash", "Alpha", "", 1)
        assert "1 message queued" in result

    def test_no_tool_shows_thinking(self):
        from hive_slack.slack import _render_todo_status

        todos = [
            {"content": "Task", "status": "in_progress", "activeForm": "Working"},
        ]
        result = _render_todo_status(todos, "", "Alpha", "", 0)
        assert "Thinking" in result

    def test_header_without_duration(self):
        from hive_slack.slack import _render_todo_status

        todos = [
            {"content": "Task", "status": "in_progress", "activeForm": "Working"},
        ]
        result = _render_todo_status(todos, "bash", "Alpha", "", 0)
        lines = result.split("\n")
        # Header should be just the name, no trailing " · "
        assert lines[0] == "⚙️ Alpha"

    def test_two_or_fewer_completed_shown_individually(self):
        from hive_slack.slack import _render_todo_status

        todos = [
            {"content": "First done", "status": "completed", "activeForm": "First done"},
            {"content": "Second done", "status": "completed", "activeForm": "Second done"},
            {"content": "Current", "status": "in_progress", "activeForm": "Working"},
        ]
        result = _render_todo_status(todos, "bash", "Alpha", "", 0)
        # With only 2 completed, show them individually (no collapse)
        assert "First done" in result
        assert "Second done" in result
        assert "completed" not in result  # No "N completed" collapse line
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/test_slack.py::TestRenderTodoStatus -v`

Expected: FAIL — `ImportError: cannot import name '_render_todo_status' from 'hive_slack.slack'`

**Step 3: Implement `_render_todo_status`**

In `src/hive_slack/slack.py`, insert the following function immediately after `_format_duration` (and before `class SlackConnector:`):

```python


def _render_todo_status(
    todos: list[dict],
    current_tool: str,
    instance_name: str,
    duration_str: str,
    queued: int,
) -> str:
    """Render plan-mode status message with todo list.

    Produces a plain mrkdwn (not Block Kit) multi-line status:
    - Header: instance name + optional duration
    - Separator line
    - Todo items: completed (✅), in-progress (▸ bold), pending (○)
    - Footer: current tool + progress count + optional queued count

    Long lists are truncated: >2 completed collapse to a count,
    >2 pending collapse with "+N more".
    """
    lines: list[str] = []

    # Header
    header = f"⚙️ {instance_name}"
    if duration_str:
        header += f" · {duration_str}"
    lines.append(header)
    lines.append("───────────────────────────────────────")

    # Categorize items
    completed = [t for t in todos if t.get("status") == "completed"]
    in_progress = [t for t in todos if t.get("status") == "in_progress"]
    pending = [t for t in todos if t.get("status") == "pending"]

    # Completed: collapse if more than 2
    if len(completed) > 2:
        lines.append(f"✅  {len(completed)} completed")
    else:
        for t in completed:
            lines.append(f"✅  {t.get('content', '')}")

    # In-progress: always show with activeForm, bolded
    for t in in_progress:
        active = t.get("activeForm", t.get("content", ""))
        lines.append(f"▸  *{active}*")

    # Pending: show first 2, collapse rest
    for t in pending[:2]:
        lines.append(f"○  {t.get('content', '')}")
    if len(pending) > 2:
        lines.append(f"    +{len(pending) - 2} more")

    # Footer: current tool + progress + queued
    total = len(todos)
    done = len(completed)
    tool_friendly = _friendly_tool_name(current_tool) if current_tool else "Thinking"
    footer = f"🔧 {tool_friendly} · {done} of {total} complete"
    if queued > 0:
        footer += f" · {queued} message{'s' if queued != 1 else ''} queued"
    lines.append(footer)

    return "\n".join(lines)
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/test_slack.py::TestRenderTodoStatus -v`

Expected: 8 PASSED

**Step 5: Run full test suite to verify no regressions**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/ -v`

Expected: 203 prior + 8 new = 211 PASSED

**Step 6: Commit**

```
feat: add _render_todo_status() helper for plan-mode display
```

---

## Task 3: Enhance `on_tool_post` in `service.py` to extract todo data and delegate agent

**Files:**
- Modify: `src/hive_slack/service.py:220-227` (the `on_tool_post` function)
- Test: `tests/test_service.py` (append new test class)

**Step 1: Write failing tests**

First, read the current `tests/test_service.py` imports and structure to follow its conventions. Then append the following test class to the end of `tests/test_service.py`:

```python
class TestOnToolPostPayloadExtraction:
    """Test that on_tool_post extracts todo and delegate data from hook events."""

    @pytest.mark.asyncio
    async def test_todo_items_forwarded_from_tool_input(self):
        """When tool_name is 'todo', todos from tool_input are forwarded."""
        from amplifier_core.models import HookResult

        captured = {}

        async def capture_callback(event_type: str, data: dict) -> None:
            captured["event_type"] = event_type
            captured["data"] = data

        service = HiveSlackService.__new__(HiveSlackService)

        mock_session = MagicMock()
        mock_hooks = MagicMock()
        mock_session.coordinator = {"hooks": mock_hooks}
        mock_hooks.get = None
        mock_session.coordinator.get = lambda key: mock_hooks if key == "hooks" else None

        # Capture the registered on_tool_post handler
        handlers = {}
        def fake_register(event, handler, priority=0, name=""):
            handlers[event] = handler
            return lambda: None
        mock_hooks.register = fake_register
        mock_hooks.__class__ = type(mock_hooks)  # ensure hasattr works

        service._register_progress_hooks(mock_session, capture_callback)

        on_tool_post = handlers["tool:post"]

        todo_items = [
            {"content": "Task 1", "status": "completed", "activeForm": "Task 1"},
            {"content": "Task 2", "status": "in_progress", "activeForm": "Doing Task 2"},
        ]
        result = await on_tool_post("tool:post", {
            "tool_name": "todo",
            "tool_input": {"action": "update", "todos": todo_items},
        })
        assert result.action == "continue"
        assert captured["data"]["tool"] == "todo"
        assert captured["data"]["todos"] == todo_items

    @pytest.mark.asyncio
    async def test_non_todo_tool_has_no_todos_key(self):
        """Non-todo tools should not have a 'todos' key in the payload."""
        captured = {}

        async def capture_callback(event_type: str, data: dict) -> None:
            captured["data"] = data

        service = HiveSlackService.__new__(HiveSlackService)

        mock_session = MagicMock()
        mock_hooks = MagicMock()
        mock_session.coordinator = {"hooks": mock_hooks}
        mock_session.coordinator.get = lambda key: mock_hooks if key == "hooks" else None

        handlers = {}
        def fake_register(event, handler, priority=0, name=""):
            handlers[event] = handler
            return lambda: None
        mock_hooks.register = fake_register

        service._register_progress_hooks(mock_session, capture_callback)

        on_tool_post = handlers["tool:post"]
        await on_tool_post("tool:post", {"tool_name": "read_file"})
        assert "todos" not in captured["data"]

    @pytest.mark.asyncio
    async def test_delegate_agent_forwarded_in_post(self):
        """When tool_name is 'delegate', agent name is forwarded."""
        captured = {}

        async def capture_callback(event_type: str, data: dict) -> None:
            captured["data"] = data

        service = HiveSlackService.__new__(HiveSlackService)

        mock_session = MagicMock()
        mock_hooks = MagicMock()
        mock_session.coordinator = {"hooks": mock_hooks}
        mock_session.coordinator.get = lambda key: mock_hooks if key == "hooks" else None

        handlers = {}
        def fake_register(event, handler, priority=0, name=""):
            handlers[event] = handler
            return lambda: None
        mock_hooks.register = fake_register

        service._register_progress_hooks(mock_session, capture_callback)

        on_tool_post = handlers["tool:post"]
        await on_tool_post("tool:post", {
            "tool_name": "delegate",
            "tool_input": {"agent": "foundation:explorer", "instruction": "look around"},
        })
        assert captured["data"]["agent"] == "foundation:explorer"

    @pytest.mark.asyncio
    async def test_todo_tool_input_as_json_string(self):
        """When tool_input arrives as a JSON string (not dict), it's parsed."""
        import json

        captured = {}

        async def capture_callback(event_type: str, data: dict) -> None:
            captured["data"] = data

        service = HiveSlackService.__new__(HiveSlackService)

        mock_session = MagicMock()
        mock_hooks = MagicMock()
        mock_session.coordinator = {"hooks": mock_hooks}
        mock_session.coordinator.get = lambda key: mock_hooks if key == "hooks" else None

        handlers = {}
        def fake_register(event, handler, priority=0, name=""):
            handlers[event] = handler
            return lambda: None
        mock_hooks.register = fake_register

        service._register_progress_hooks(mock_session, capture_callback)

        todo_items = [{"content": "Task", "status": "pending", "activeForm": "Task"}]
        on_tool_post = handlers["tool:post"]
        await on_tool_post("tool:post", {
            "tool_name": "todo",
            "tool_input": json.dumps({"action": "create", "todos": todo_items}),
        })
        assert captured["data"]["todos"] == todo_items
```

> **Note to implementer:** Check the imports at the top of `tests/test_service.py`. You will need `HiveSlackService` imported — check the existing imports and add if missing. The pattern `service = HiveSlackService.__new__(HiveSlackService)` avoids calling `__init__`. If the existing tests use a different pattern (e.g., a fixture), match that pattern instead. The key is to get a real instance so `_register_progress_hooks` binds correctly, then call the captured handler.

**Step 2: Run tests to verify they fail**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/test_service.py::TestOnToolPostPayloadExtraction -v`

Expected: FAIL — the current `on_tool_post` only sends `{"tool": tool_name}`, no `todos` or `agent` keys.

**Step 3: Implement enhanced `on_tool_post`**

In `src/hive_slack/service.py`, replace the `on_tool_post` function (lines 220–227) with:

```python
        async def on_tool_post(event_name: str, data: dict[str, Any]) -> HookResult:
            tool_name = data.get("tool_name", "")
            logger.info("Progress hook fired: tool:post → %s", tool_name)

            payload: dict[str, Any] = {"tool": tool_name}

            # Extract todo data when the todo tool is called
            if tool_name == "todo":
                tool_input = data.get("tool_input", {})
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except Exception:
                        tool_input = {}
                todos = tool_input.get("todos") if isinstance(tool_input, dict) else None
                if todos is None:
                    # Fallback: check result output (for "list" action)
                    result = data.get("result", {})
                    output = result.get("output", {}) if isinstance(result, dict) else {}
                    todos = output.get("todos") if isinstance(output, dict) else None
                if todos:
                    payload["todos"] = todos

            # Extract delegate agent name
            if tool_name == "delegate":
                tool_input = data.get("tool_input", {})
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except Exception:
                        tool_input = {}
                agent = tool_input.get("agent", "") if isinstance(tool_input, dict) else ""
                if agent:
                    payload["agent"] = agent

            try:
                await callback("tool:end", payload)
            except Exception:
                logger.warning("Progress callback error for tool:end", exc_info=True)
            return HookResult(action="continue")
```

> **Note:** `json` is already imported at the top of `service.py` (line 5). No new imports needed.

**Step 4: Run tests to verify they pass**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/test_service.py::TestOnToolPostPayloadExtraction -v`

Expected: 4 PASSED

**Step 5: Run full test suite to verify no regressions**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/ -v`

Expected: 211 prior + 4 new = 215 PASSED

**Step 6: Commit**

```
feat: extract todo items and delegate agent from tool:post hook events
```

---

## Task 4: Enhance `on_tool_pre` in `service.py` to extract delegate agent

**Files:**
- Modify: `src/hive_slack/service.py:211-218` (the `on_tool_pre` function)
- Test: `tests/test_service.py` (append to existing or new test class)

**Step 1: Write failing tests**

Append the following test class to the end of `tests/test_service.py`:

```python
class TestOnToolPrePayloadExtraction:
    """Test that on_tool_pre extracts delegate agent from hook events."""

    @pytest.mark.asyncio
    async def test_delegate_agent_forwarded_in_pre(self):
        """When tool_name is 'delegate', agent name is forwarded in tool:start."""
        captured = {}

        async def capture_callback(event_type: str, data: dict) -> None:
            if event_type == "tool:start":
                captured["data"] = data

        service = HiveSlackService.__new__(HiveSlackService)

        mock_session = MagicMock()
        mock_hooks = MagicMock()
        mock_session.coordinator = {"hooks": mock_hooks}
        mock_session.coordinator.get = lambda key: mock_hooks if key == "hooks" else None

        handlers = {}
        def fake_register(event, handler, priority=0, name=""):
            handlers[event] = handler
            return lambda: None
        mock_hooks.register = fake_register

        service._register_progress_hooks(mock_session, capture_callback)

        on_tool_pre = handlers["tool:pre"]
        await on_tool_pre("tool:pre", {
            "tool_name": "delegate",
            "tool_input": {"agent": "foundation:bug-hunter", "instruction": "fix it"},
        })
        assert captured["data"]["tool"] == "delegate"
        assert captured["data"]["agent"] == "foundation:bug-hunter"

    @pytest.mark.asyncio
    async def test_non_delegate_tool_has_no_agent_key(self):
        """Non-delegate tools should not have an 'agent' key in the payload."""
        captured = {}

        async def capture_callback(event_type: str, data: dict) -> None:
            if event_type == "tool:start":
                captured["data"] = data

        service = HiveSlackService.__new__(HiveSlackService)

        mock_session = MagicMock()
        mock_hooks = MagicMock()
        mock_session.coordinator = {"hooks": mock_hooks}
        mock_session.coordinator.get = lambda key: mock_hooks if key == "hooks" else None

        handlers = {}
        def fake_register(event, handler, priority=0, name=""):
            handlers[event] = handler
            return lambda: None
        mock_hooks.register = fake_register

        service._register_progress_hooks(mock_session, capture_callback)

        on_tool_pre = handlers["tool:pre"]
        await on_tool_pre("tool:pre", {"tool_name": "read_file", "tool_input": {}})
        assert "agent" not in captured["data"]

    @pytest.mark.asyncio
    async def test_delegate_tool_input_as_json_string(self):
        """When tool_input arrives as a JSON string, it's parsed for agent."""
        import json

        captured = {}

        async def capture_callback(event_type: str, data: dict) -> None:
            if event_type == "tool:start":
                captured["data"] = data

        service = HiveSlackService.__new__(HiveSlackService)

        mock_session = MagicMock()
        mock_hooks = MagicMock()
        mock_session.coordinator = {"hooks": mock_hooks}
        mock_session.coordinator.get = lambda key: mock_hooks if key == "hooks" else None

        handlers = {}
        def fake_register(event, handler, priority=0, name=""):
            handlers[event] = handler
            return lambda: None
        mock_hooks.register = fake_register

        service._register_progress_hooks(mock_session, capture_callback)

        on_tool_pre = handlers["tool:pre"]
        await on_tool_pre("tool:pre", {
            "tool_name": "delegate",
            "tool_input": json.dumps({"agent": "self", "instruction": "think"}),
        })
        assert captured["data"]["agent"] == "self"
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/test_service.py::TestOnToolPrePayloadExtraction -v`

Expected: FAIL — the current `on_tool_pre` only sends `{"tool": tool_name}`, no `agent` key.

**Step 3: Implement enhanced `on_tool_pre`**

In `src/hive_slack/service.py`, replace the `on_tool_pre` function (lines 211–218) with:

```python
        async def on_tool_pre(event_name: str, data: dict[str, Any]) -> HookResult:
            tool_name = data.get("tool_name", "")
            logger.info("Progress hook fired: tool:pre → %s", tool_name)

            payload: dict[str, Any] = {"tool": tool_name}

            # Extract delegate agent name
            if tool_name == "delegate":
                tool_input = data.get("tool_input", {})
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except Exception:
                        tool_input = {}
                agent = tool_input.get("agent", "") if isinstance(tool_input, dict) else ""
                if agent:
                    payload["agent"] = agent

            try:
                await callback("tool:start", payload)
            except Exception:
                logger.warning("Progress callback error for tool:start", exc_info=True)
            return HookResult(action="continue")
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/test_service.py::TestOnToolPrePayloadExtraction -v`

Expected: 3 PASSED

**Step 5: Run full test suite to verify no regressions**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/ -v`

Expected: 215 prior + 3 new = 218 PASSED

**Step 6: Commit**

```
feat: extract delegate agent name from tool:pre hook events
```

---

## Task 5: Refactor `on_progress` closure to adaptive two-mode rendering

**Files:**
- Modify: `src/hive_slack/slack.py:438-507` (the `start_time` variable and `on_progress` closure)
- Test: `tests/test_slack.py` (append new test class)

This is the largest task. It replaces the existing `on_progress` closure with one that maintains state and renders adaptively.

**Step 1: Write failing tests**

Append the following test class to the end of `tests/test_slack.py`:

```python
class TestOnProgressAdaptiveRendering:
    """Test the adaptive on_progress closure in _execute_with_progress."""

    @pytest.fixture
    def connector(self, make_config):
        """Create a SlackConnector with mocked Slack app."""
        config = make_config()
        connector = SlackConnector.__new__(SlackConnector)
        connector._config = config
        connector._app = MagicMock()
        connector._app.client = MagicMock()
        connector._app.client.chat_postMessage = AsyncMock(return_value={"ts": "status.123"})
        connector._app.client.chat_update = AsyncMock()
        connector._app.client.chat_delete = AsyncMock()
        connector._app.client.reactions_add = AsyncMock()
        connector._app.client.reactions_remove = AsyncMock()
        connector._active_executions = {}
        connector._message_queues = {}
        connector._thread_owners = {}
        return connector

    @pytest.mark.asyncio
    async def test_simple_mode_shows_tool_name(self, connector):
        """Without todos, status shows simple tool name."""
        mock_service = AsyncMock()
        # Capture the on_progress callback passed to service.execute
        on_progress_ref = {}

        async def fake_execute(session_id, prompt, *, on_progress=None, **kw):
            on_progress_ref["fn"] = on_progress
            if on_progress:
                await on_progress("tool:start", {"tool": "read_file"})
                # Wait a moment to bypass throttle
                import time as _time
                # Manually advance past throttle by manipulating state
                # (The throttle checks monotonic time; we just call twice with enough gap)
                await on_progress("tool:end", {"tool": "read_file"})
            return "done"

        mock_service.execute = fake_execute

        instance = MagicMock()
        instance.persona.name = "Alpha"

        await connector._execute_with_progress(
            instance_name="alpha",
            instance=instance,
            conversation_id="conv1",
            prompt="hello",
            channel="C123",
            thread_ts="thread.1",
            user_ts="user.1",
            say=AsyncMock(),
        )

        # chat_update should have been called with simple mode text
        calls = connector._app.client.chat_update.call_args_list
        # At least one call should contain the friendly tool name
        texts = [str(c) for c in calls]
        found_reading = any("Reading files" in str(c) for c in calls)
        assert found_reading, f"Expected 'Reading files' in status updates. Calls: {texts}"

    @pytest.mark.asyncio
    async def test_plan_mode_triggered_by_todos(self, connector):
        """When tool:end includes todos, status switches to plan mode."""
        mock_service = AsyncMock()

        async def fake_execute(session_id, prompt, *, on_progress=None, **kw):
            if on_progress:
                # First: simple tool call
                await on_progress("tool:start", {"tool": "read_file"})
                await on_progress("tool:end", {"tool": "read_file"})
                # Then: todo tool returns todos → triggers plan mode
                todo_items = [
                    {"content": "Read code", "status": "completed", "activeForm": "Reading code"},
                    {"content": "Write impl", "status": "in_progress", "activeForm": "Writing impl"},
                    {"content": "Test", "status": "pending", "activeForm": "Testing"},
                ]
                await on_progress("tool:end", {"tool": "todo", "todos": todo_items})
            return "done"

        mock_service.execute = fake_execute

        instance = MagicMock()
        instance.persona.name = "Alpha"

        await connector._execute_with_progress(
            instance_name="alpha",
            instance=instance,
            conversation_id="conv1",
            prompt="hello",
            channel="C123",
            thread_ts="thread.1",
            user_ts="user.1",
            say=AsyncMock(),
        )

        # Check that at least one chat_update call contains plan-mode indicators
        calls = connector._app.client.chat_update.call_args_list
        texts = [str(c) for c in calls]
        found_plan = any("✅" in str(c) and "▸" in str(c) for c in calls)
        assert found_plan, f"Expected plan-mode markers (✅, ▸) in updates. Calls: {texts}"

    @pytest.mark.asyncio
    async def test_delegate_shows_agent_name(self, connector):
        """When delegating, status shows the agent name."""
        mock_service = AsyncMock()

        async def fake_execute(session_id, prompt, *, on_progress=None, **kw):
            if on_progress:
                await on_progress("tool:start", {"tool": "delegate", "agent": "foundation:explorer"})
            return "done"

        mock_service.execute = fake_execute

        instance = MagicMock()
        instance.persona.name = "Alpha"

        await connector._execute_with_progress(
            instance_name="alpha",
            instance=instance,
            conversation_id="conv1",
            prompt="hello",
            channel="C123",
            thread_ts="thread.1",
            user_ts="user.1",
            say=AsyncMock(),
        )

        calls = connector._app.client.chat_update.call_args_list
        found_delegate = any("foundation:explorer" in str(c) for c in calls)
        assert found_delegate, f"Expected 'foundation:explorer' in updates. Calls: {calls}"
```

> **Important note to implementer:** The `connector` fixture above creates a `SlackConnector` via `__new__` to avoid real Slack initialization. Check how existing `TestProgressIndicators` tests set up their connector — they likely use a slightly different pattern. **Match the existing pattern.** The key requirement: `service.execute` must accept an `on_progress` keyword argument. Check what the real `service.execute()` signature looks like in the connector's call site (around line 517–525 in `slack.py`). The `fake_execute` in the tests must match that signature. If `connector._execute_with_progress` calls `self._service.execute(...)`, you'll need to set `connector._service = mock_service` in the fixture setup.

**Step 2: Run tests to verify they fail**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/test_slack.py::TestOnProgressAdaptiveRendering -v`

Expected: FAIL — the current `on_progress` doesn't handle `todos` in data, doesn't look for `agent`, and shows "done. Thinking..." instead of adaptive rendering.

**Step 3: Implement the refactored `on_progress` closure**

In `src/hive_slack/slack.py`, make the following changes:

**3a.** Replace lines 438–440 (the `import time` and `start_time` variable):

Find this block:
```python
        import time as _time

        start_time = _time.monotonic()
```

Replace with:
```python
        import time as _time

        _start_time = _time.monotonic()
```

**3b.** Replace the entire `on_progress` closure (lines 473–507):

Find this block:
```python
        # Progress callback for service.execute()
        async def on_progress(event_type: str, data: dict) -> None:
            if not status_msg:
                return
            text = None
            if event_type == "executing":
                text = "⚙️ Working..."
            elif event_type in ("tool:pre", "tool:start"):
                tool = data.get("tool", data.get("tool_name", ""))
                friendly = _friendly_tool_name(tool)
                text = f"⚙️ {friendly}..."
            elif event_type in ("tool:post", "tool:end"):
                tool = data.get("tool", data.get("tool_name", ""))
                friendly = _friendly_tool_name(tool)
                text = f"⚙️ {friendly} done. Thinking..."
            elif event_type in ("complete", "error"):
                return  # We handle completion below

            if text:
                queued = len(self._message_queues.get(conversation_id, []))
                if queued:
                    text += f" ({queued} message{'s' if queued != 1 else ''} queued)"
                try:
                    logger.debug(
                        "Updating status message: %s (ts=%s)", text[:60], status_msg
                    )
                    await self._app.client.chat_update(
                        channel=channel,
                        ts=status_msg,
                        text=text,
                    )
                except Exception:
                    logger.debug(
                        "Failed to update status message", exc_info=True
                    )
```

Replace with:
```python
        # --- Adaptive status rendering state ---
        _status_todos: list[dict] | None = None  # None = simple mode, list = plan mode
        _status_tool: str = ""
        _status_agent: str = ""
        _last_status_update: float = 0.0
        _STATUS_THROTTLE: float = 2.0

        async def on_progress(event_type: str, data: dict) -> None:
            nonlocal _status_todos, _status_tool, _status_agent, _last_status_update
            if not status_msg:
                return

            # Update state from events
            if event_type in ("tool:pre", "tool:start"):
                _status_tool = data.get("tool", "")
                agent = data.get("agent", "")
                if agent:
                    _status_agent = agent
            elif event_type in ("tool:post", "tool:end"):
                todos = data.get("todos")
                if todos:
                    _status_todos = todos
                _status_tool = ""
                _status_agent = ""
            elif event_type in ("complete", "error"):
                return  # Completion handled below in the try/finally block

            # Throttle Slack API updates
            now = _time.monotonic()
            if now - _last_status_update < _STATUS_THROTTLE:
                return
            _last_status_update = now

            # Render based on mode
            duration_str = _format_duration(now - _start_time)
            queued = len(self._message_queues.get(conversation_id, []))

            if _status_todos is not None:
                # Plan mode: structured todo list
                text = _render_todo_status(
                    _status_todos,
                    _status_tool,
                    instance_name,
                    duration_str,
                    queued,
                )
            else:
                # Simple mode: tool name only
                if _status_tool == "delegate" and _status_agent:
                    text = f"⚙️ Delegating to {_status_agent}..."
                elif _status_tool:
                    text = f"⚙️ {_friendly_tool_name(_status_tool)}..."
                else:
                    text = "⚙️ Working..."

                if duration_str:
                    text += f" · {duration_str}"
                if queued:
                    text += f" · {queued} message{'s' if queued != 1 else ''} queued"

            try:
                logger.debug("Updating status: %s", text[:80])
                await self._app.client.chat_update(
                    channel=channel,
                    ts=status_msg,
                    text=text,
                )
            except Exception:
                logger.debug("Failed to update status message", exc_info=True)
```

**3c.** Fix the `start_time` reference used by onboarding (line 548):

Find:
```python
                duration = _time.monotonic() - start_time
```

Replace with:
```python
                duration = _time.monotonic() - _start_time
```

**Step 4: Run the new tests to verify they pass**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/test_slack.py::TestOnProgressAdaptiveRendering -v`

Expected: 3 PASSED

**Step 5: Run full test suite to verify no regressions**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/ -v`

Expected: 218 prior + 3 new = 221 PASSED

> **Regression risk:** The existing `TestProgressIndicators` tests mock `service.execute` as a direct `AsyncMock`, so the `on_progress` closure is never called in those tests. They should still pass. If any test references `start_time` (without underscore prefix), find it and update to `_start_time`.

**Step 6: Commit**

```
feat: adaptive two-mode status messages with todo list and throttling
```

---

## Task 6: Final integration verification

**Files:** None (verification only)

**Step 1: Run the full test suite one final time**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m pytest tests/ -v`

Expected: ~221 tests PASSED, 0 FAILED

**Step 2: Run code quality checks**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m ruff check src/hive_slack/service.py src/hive_slack/slack.py`

Expected: No errors

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && .venv/bin/python -m ruff format --check src/hive_slack/service.py src/hive_slack/slack.py`

Expected: Files already formatted (or format them if needed)

**Step 3: Verify no debug artifacts left behind**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && grep -rn "breakpoint\|pdb\|print(" src/hive_slack/service.py src/hive_slack/slack.py`

Expected: No matches (or only intentional `print` calls in unrelated code)

**Step 4: Review the diff**

Run: `cd /home/bkrabach/dev/slack-connector/amplifier-hive-slack && git diff --stat`

Expected output should show approximately:
```
 src/hive_slack/service.py | ~40 lines changed
 src/hive_slack/slack.py   | ~80 lines changed
 tests/test_service.py     | ~120 lines added
 tests/test_slack.py       | ~180 lines added
```

**Step 5: Final commit (if any formatting fixes were needed)**

```
style: format service.py and slack.py
```

---

## Summary

| Task | What | Files | New Tests |
|------|------|-------|-----------|
| 1 | `_format_duration()` helper | `slack.py`, `test_slack.py` | 4 |
| 2 | `_render_todo_status()` helper | `slack.py`, `test_slack.py` | 8 |
| 3 | Enhanced `on_tool_post` (todo + delegate) | `service.py`, `test_service.py` | 4 |
| 4 | Enhanced `on_tool_pre` (delegate) | `service.py`, `test_service.py` | 3 |
| 5 | Adaptive `on_progress` closure | `slack.py`, `test_slack.py` | 3 |
| 6 | Integration verification | — | 0 |
| **Total** | | **4 files** | **~22 new tests** |

### Key Design Decisions

- **One-way mode transition:** `_status_todos` starts as `None` (simple mode). Once a `tool:end` event carries `todos`, it's set to a list and stays in plan mode permanently for that execution.
- **2-second throttle:** Prevents Slack API rate limiting. Every event updates internal state, but `chat_update` is only called if ≥2s since last update.
- **`start_time` renamed to `_start_time`:** The underscore prefix signals it's part of the status rendering state. The onboarding `duration` calculation (line 548) is updated to match.
- **No Block Kit:** All rendering uses plain mrkdwn text, keeping `chat_update` calls simple.
- **Pure functions for rendering:** `_format_duration` and `_render_todo_status` are module-level pure functions — easy to test independently of Slack API mocking.
