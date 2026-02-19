"""Behavioral tests for the vendored InteractiveOrchestrator.

Tests cover: text responses, tool execution, injection queue,
force-respond, extended thinking, error handling, and cancellation.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Insert vendored module path so we can import the orchestrator directly
sys.path.insert(0, str(Path(__file__).parent.parent / "modules" / "loop-interactive"))

from amplifier_core import ChatResponse, HookRegistry, MockContextManager, MockTool
from amplifier_core.message_models import TextBlock, ThinkingBlock, ToolCall

from amplifier_module_loop_interactive import InteractiveOrchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeProvider:
    """Minimal provider stub without a ``stream`` attribute.

    The orchestrator checks ``hasattr(provider, "stream")`` to choose between
    streaming and non-streaming paths.  MagicMock auto-creates attributes on
    access, which would send us down the streaming path.  A plain class avoids
    that problem entirely.
    """

    def __init__(self):
        self.name = "test-provider"
        self.priority = 0
        self.complete = AsyncMock()
        self.parse_tool_calls = MagicMock(return_value=[])
        self.get_info = MagicMock(return_value=MagicMock(context_window=100_000))


def _make_orchestrator(**overrides) -> InteractiveOrchestrator:
    config = {"stream_delay": 0, **overrides}
    return InteractiveOrchestrator(config)


def _make_provider(responses=None, tool_calls_seq=None) -> FakeProvider:
    """Build a FakeProvider with pre-loaded response sequences."""
    p = FakeProvider()
    if responses is not None:
        p.complete = AsyncMock(side_effect=list(responses))
    if tool_calls_seq is not None:
        p.parse_tool_calls = MagicMock(side_effect=list(tool_calls_seq))
    return p


def _providers(provider: FakeProvider) -> dict:
    return {"test-provider": provider}


# ---------------------------------------------------------------------------
# TestTextResponse -- happy-path text-only completions
# ---------------------------------------------------------------------------


class TestTextResponse:
    """Provider returns text, no tool calls."""

    @pytest.mark.asyncio
    async def test_simple_text_response(self):
        """execute() collects streamed tokens and returns concatenated text."""
        provider = _make_provider(
            responses=[ChatResponse(content=[TextBlock(text="Hello world")])],
            tool_calls_seq=[[]],
        )
        orch = _make_orchestrator()
        result = await orch.execute(
            "Hi", MockContextManager(), _providers(provider), {}, HookRegistry()
        )
        assert result == "Hello world"

    @pytest.mark.asyncio
    async def test_multiline_text_preserved(self):
        """Newlines survive the tokenize-stream round-trip."""
        text = "Line one\nLine two\nLine three"
        provider = _make_provider(
            responses=[ChatResponse(content=[TextBlock(text=text)])],
            tool_calls_seq=[[]],
        )
        orch = _make_orchestrator()
        result = await orch.execute(
            "Hi", MockContextManager(), _providers(provider), {}, HookRegistry()
        )
        assert result == text

    @pytest.mark.asyncio
    async def test_text_extracted_from_multiple_content_blocks(self):
        """Multiple TextBlocks are joined with double-newline separator."""
        provider = _make_provider(
            responses=[
                ChatResponse(
                    content=[TextBlock(text="Part A"), TextBlock(text="Part B")]
                )
            ],
            tool_calls_seq=[[]],
        )
        orch = _make_orchestrator()
        result = await orch.execute(
            "Hi", MockContextManager(), _providers(provider), {}, HookRegistry()
        )
        assert result == "Part A\n\nPart B"


# ---------------------------------------------------------------------------
# TestToolExecution -- tool call loop
# ---------------------------------------------------------------------------


class TestToolExecution:
    """Provider returns tool calls, orchestrator executes them, then gets text."""

    @pytest.mark.asyncio
    async def test_single_tool_call_then_text(self):
        """One tool call iteration followed by a text-only response."""
        tc = ToolCall(id="tc_1", name="echo", arguments={"input": "ping"})
        provider = _make_provider(
            responses=[
                ChatResponse(content=[TextBlock(text="Using tool")], tool_calls=[tc]),
                ChatResponse(content=[TextBlock(text="Done")]),
            ],
            tool_calls_seq=[[tc], []],
        )
        tools = {"echo": MockTool("echo", "pong")}
        orch = _make_orchestrator()

        result = await orch.execute(
            "Do it", MockContextManager(), _providers(provider), tools, HookRegistry()
        )
        assert result == "Done"
        assert provider.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_parallel_tool_calls(self):
        """Multiple tool calls in one response are executed concurrently."""
        tc1 = ToolCall(id="tc_1", name="alpha", arguments={})
        tc2 = ToolCall(id="tc_2", name="beta", arguments={})
        provider = _make_provider(
            responses=[
                ChatResponse(
                    content=[TextBlock(text="Calling both")],
                    tool_calls=[tc1, tc2],
                ),
                ChatResponse(content=[TextBlock(text="All done")]),
            ],
            tool_calls_seq=[[tc1, tc2], []],
        )
        tools = {
            "alpha": MockTool("alpha", "a-result"),
            "beta": MockTool("beta", "b-result"),
        }
        orch = _make_orchestrator()

        result = await orch.execute(
            "Go", MockContextManager(), _providers(provider), tools, HookRegistry()
        )
        assert result == "All done"

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_message(self):
        """A tool call for a tool that doesn't exist yields an error string
        in the tool result, and the loop continues."""
        tc = ToolCall(id="tc_1", name="no_such_tool", arguments={})
        provider = _make_provider(
            responses=[
                ChatResponse(content=[TextBlock(text="")], tool_calls=[tc]),
                ChatResponse(content=[TextBlock(text="Recovered")]),
            ],
            tool_calls_seq=[[tc], []],
        )
        tools = {}  # no tools available
        ctx = MockContextManager()
        orch = _make_orchestrator()

        result = await orch.execute(
            "Try", ctx, _providers(provider), tools, HookRegistry()
        )
        # Orchestrator continues after unknown tool and gets final text
        assert result == "Recovered"
        # Verify the error tool result was added to context
        messages = await ctx.get_messages_for_request()
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert any("not found" in m["content"] for m in tool_msgs)


# ---------------------------------------------------------------------------
# TestInjectionQueue -- mid-execution message injection
# ---------------------------------------------------------------------------


class TestInjectionQueue:
    """inject_message() pushes to the queue; drain points pick them up."""

    @pytest.mark.asyncio
    async def test_injection_point_1_at_iteration_start(self):
        """Messages injected after execute() starts are drained at the top
        of the first iteration (injection point 1).

        Note: execute() clears the queue on entry, so we inject via the
        ``execution:start`` hook which fires just before the loop begins.
        """
        provider = _make_provider(
            responses=[ChatResponse(content=[TextBlock(text="Got it")])],
            tool_calls_seq=[[]],
        )
        ctx = MockContextManager()
        orch = _make_orchestrator()
        hooks = HookRegistry()

        # Use execution:start hook to inject AFTER the queue is cleared
        # but BEFORE the loop's first injection-point-1 check.
        async def inject_on_start(event_name, event_data):
            orch.inject_message("urgent follow-up")

        hooks.on("execution:start", inject_on_start)

        await orch.execute("Hi", ctx, _providers(provider), {}, hooks)

        # The injected message should appear in context as a user message
        messages = await ctx.get_messages_for_request()
        injected = [
            m
            for m in messages
            if m.get("role") == "user" and "urgent follow-up" in m.get("content", "")
        ]
        assert len(injected) == 1
        assert "additional messages while you were working" in injected[0]["content"]

    @pytest.mark.asyncio
    async def test_injection_point_2_prevents_break(self):
        """If messages arrive while the LLM was thinking (no tool calls),
        injection point 2 fires and the loop continues instead of breaking."""
        first_response = ChatResponse(content=[TextBlock(text="First ")])
        second_response = ChatResponse(content=[TextBlock(text="Second")])
        orch = _make_orchestrator()

        call_count = 0

        async def complete_with_injection(chat_request, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate a message arriving while LLM was thinking
                orch.inject_message("also consider this")
                return first_response
            return second_response

        provider = FakeProvider()
        provider.complete = AsyncMock(side_effect=complete_with_injection)
        provider.parse_tool_calls = MagicMock(return_value=[])

        result = await orch.execute(
            "Hi", MockContextManager(), _providers(provider), {}, HookRegistry()
        )
        # Both responses should be collected because injection point 2
        # caused a continue instead of break after the first response
        assert "First " in result
        assert "Second" in result
        assert provider.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_multiple_injections_combined(self):
        """Multiple queued messages are combined into one user message."""
        provider = _make_provider(
            responses=[ChatResponse(content=[TextBlock(text="OK")])],
            tool_calls_seq=[[]],
        )
        ctx = MockContextManager()
        orch = _make_orchestrator()
        hooks = HookRegistry()

        # Inject via hook (execute() clears the queue on entry)
        async def inject_multiple(event_name, event_data):
            orch.inject_message("message one")
            orch.inject_message("message two")

        hooks.on("execution:start", inject_multiple)

        await orch.execute("Hi", ctx, _providers(provider), {}, hooks)

        messages = await ctx.get_messages_for_request()
        injected = [
            m
            for m in messages
            if m.get("role") == "user" and "additional messages" in m.get("content", "")
        ]
        assert len(injected) == 1
        assert "- message one" in injected[0]["content"]
        assert "- message two" in injected[0]["content"]

    @pytest.mark.asyncio
    async def test_injection_point_3_after_tool_execution(self):
        """Messages queued during tool execution are drained at injection
        point 3 (after tool results are added to context)."""
        tc = ToolCall(id="tc_1", name="slow_tool", arguments={})
        first_response = ChatResponse(
            content=[TextBlock(text="Calling tool")], tool_calls=[tc]
        )
        final_response = ChatResponse(content=[TextBlock(text="Final")])
        orch = _make_orchestrator()

        call_count = 0

        async def complete_side_effect(chat_request, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_response
            return final_response

        provider = FakeProvider()
        provider.complete = AsyncMock(side_effect=complete_side_effect)
        provider.parse_tool_calls = MagicMock(side_effect=[[tc], []])

        # The tool's execute will inject a message (simulating user sending
        # a message while a tool is running)
        original_tool = MockTool("slow_tool", "tool result")
        real_execute = original_tool.execute

        async def execute_and_inject(args):
            orch.inject_message("sent during tool run")
            return await real_execute(args)

        original_tool.execute = execute_and_inject
        tools = {"slow_tool": original_tool}
        ctx = MockContextManager()

        await orch.execute("Go", ctx, _providers(provider), tools, HookRegistry())

        # Verify the injected message ended up in context
        messages = await ctx.get_messages_for_request()
        injected = [
            m
            for m in messages
            if m.get("role") == "user"
            and "sent during tool run" in m.get("content", "")
        ]
        assert len(injected) == 1


# ---------------------------------------------------------------------------
# TestForceRespond -- dispatch_worker tool stripping
# ---------------------------------------------------------------------------


class TestForceRespond:
    """After dispatch_worker runs, tools are stripped for the next LLM call."""

    @pytest.mark.asyncio
    async def test_dispatch_worker_strips_tools_on_next_call(self):
        """When dispatch_worker is among tool results, the next ChatRequest
        has tools=None so the LLM must produce a text response."""
        tc = ToolCall(id="tc_1", name="dispatch_worker", arguments={"task": "x"})
        provider = _make_provider(
            responses=[
                ChatResponse(content=[TextBlock(text="Dispatching")], tool_calls=[tc]),
                ChatResponse(content=[TextBlock(text="Worker dispatched")]),
            ],
            tool_calls_seq=[[tc], []],
        )
        tools = {"dispatch_worker": MockTool("dispatch_worker", "dispatched")}
        orch = _make_orchestrator()

        result = await orch.execute(
            "Send it", MockContextManager(), _providers(provider), tools, HookRegistry()
        )
        assert result == "Worker dispatched"

        # The SECOND call to provider.complete should have tools=None
        second_call_request = provider.complete.call_args_list[1][0][0]
        assert second_call_request.tools is None

    @pytest.mark.asyncio
    async def test_force_respond_resets_after_one_call(self):
        """The force-respond flag is a one-shot: tools are stripped only for
        the immediately-following LLM call."""
        tc_dispatch = ToolCall(id="tc_1", name="dispatch_worker", arguments={})

        provider = _make_provider(
            responses=[
                # 1st: dispatch_worker tool call
                ChatResponse(content=[TextBlock(text="")], tool_calls=[tc_dispatch]),
                # 2nd: force-respond (no tools) -> LLM produces text, loop breaks
                ChatResponse(content=[TextBlock(text="Acknowledged")]),
            ],
            tool_calls_seq=[[tc_dispatch], []],
        )
        tools = {
            "dispatch_worker": MockTool("dispatch_worker", "ok"),
            "echo": MockTool("echo", "echo-out"),
        }
        orch = _make_orchestrator()
        await orch.execute(
            "Go", MockContextManager(), _providers(provider), tools, HookRegistry()
        )

        # 1st call: tools present
        first_request = provider.complete.call_args_list[0][0][0]
        assert first_request.tools is not None
        # 2nd call: tools stripped (force-respond)
        second_request = provider.complete.call_args_list[1][0][0]
        assert second_request.tools is None

    @pytest.mark.asyncio
    async def test_force_respond_tools_configurable(self):
        """force_respond_tools config adds custom tools to the set."""
        tc = ToolCall(id="tc_1", name="recipes", arguments={})
        provider = _make_provider(
            responses=[
                ChatResponse(content=[TextBlock(text="")], tool_calls=[tc]),
                ChatResponse(content=[TextBlock(text="Recipe started")]),
            ],
            tool_calls_seq=[[tc], []],
        )
        tools = {"recipes": MockTool("recipes", "ok")}
        orch = _make_orchestrator(force_respond_tools=["dispatch_worker", "recipes"])

        result = await orch.execute(
            "Go", MockContextManager(), _providers(provider), tools, HookRegistry()
        )
        assert result == "Recipe started"

        # 2nd call should have tools stripped (force-respond triggered by "recipes")
        second_request = provider.complete.call_args_list[1][0][0]
        assert second_request.tools is None

    @pytest.mark.asyncio
    async def test_force_respond_default_includes_dispatch_worker(self):
        """Without config, dispatch_worker is still in force_respond_tools."""
        orch = _make_orchestrator()
        assert "dispatch_worker" in orch._force_respond_tools

    @pytest.mark.asyncio
    async def test_force_respond_config_overrides_default(self):
        """Config completely replaces the default set."""
        orch = _make_orchestrator(force_respond_tools=["my_tool"])
        assert "my_tool" in orch._force_respond_tools
        assert "dispatch_worker" not in orch._force_respond_tools


# ---------------------------------------------------------------------------
# TestExtendedThinking -- thinking blocks and empty text filtering
# ---------------------------------------------------------------------------


class TestExtendedThinking:
    """Extended thinking: kwargs, ThinkingBlock preservation, empty-text filter."""

    @pytest.mark.asyncio
    async def test_extended_thinking_kwarg_passed_to_provider(self):
        """When extended_thinking=True, provider.complete() receives the kwarg."""
        provider = _make_provider(
            responses=[ChatResponse(content=[TextBlock(text="Thought about it")])],
            tool_calls_seq=[[]],
        )
        orch = _make_orchestrator(extended_thinking=True)
        await orch.execute(
            "Think", MockContextManager(), _providers(provider), {}, HookRegistry()
        )
        _, kwargs = provider.complete.call_args
        assert kwargs.get("extended_thinking") is True

    @pytest.mark.asyncio
    async def test_thinking_block_preserved_in_context(self):
        """ThinkingBlock is stored as assistant_msg['thinking_block'] dict."""
        thinking = ThinkingBlock(thinking="Let me reason...", signature="sig123")
        response = ChatResponse(
            content=[thinking, TextBlock(text="Here is the answer")]
        )
        provider = _make_provider(responses=[response], tool_calls_seq=[[]])
        ctx = MockContextManager()
        orch = _make_orchestrator(extended_thinking=True)

        await orch.execute("Think", ctx, _providers(provider), {}, HookRegistry())

        messages = await ctx.get_messages_for_request()
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        assert "thinking_block" in assistant_msgs[0]
        tb = assistant_msgs[0]["thinking_block"]
        assert tb["type"] == "thinking"
        assert tb["thinking"] == "Let me reason..."
        assert tb["signature"] == "sig123"

    @pytest.mark.asyncio
    async def test_empty_text_block_filtered_from_content(self):
        """TextBlock(text='') is filtered out of the stored content dicts."""
        thinking = ThinkingBlock(thinking="reasoning", signature="sig456")
        empty_text = TextBlock(text="")
        response = ChatResponse(content=[thinking, empty_text])
        provider = _make_provider(responses=[response], tool_calls_seq=[[]])
        ctx = MockContextManager()
        orch = _make_orchestrator(extended_thinking=True)

        await orch.execute("Think", ctx, _providers(provider), {}, HookRegistry())

        messages = await ctx.get_messages_for_request()
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        content = assistant_msgs[0]["content"]
        # content should be a list of dicts (structured blocks)
        assert isinstance(content, list)
        # The empty text block should have been filtered out
        text_blocks = [b for b in content if b.get("type") == "text"]
        assert len(text_blocks) == 0
        # The thinking block should remain
        thinking_blocks = [b for b in content if b.get("type") == "thinking"]
        assert len(thinking_blocks) == 1

    @pytest.mark.asyncio
    async def test_extended_thinking_not_passed_when_disabled(self):
        """When extended_thinking is False (default), kwarg is not sent."""
        provider = _make_provider(
            responses=[ChatResponse(content=[TextBlock(text="Normal")])],
            tool_calls_seq=[[]],
        )
        orch = _make_orchestrator()  # extended_thinking defaults to False
        await orch.execute(
            "Hi", MockContextManager(), _providers(provider), {}, HookRegistry()
        )
        _, kwargs = provider.complete.call_args
        assert "extended_thinking" not in kwargs


# ---------------------------------------------------------------------------
# TestErrorHandling -- provider errors and max iterations
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Error paths: no providers, exceptions, empty-message errors, max iters."""

    @pytest.mark.asyncio
    async def test_no_providers_returns_error(self):
        """Empty providers dict yields an error message."""
        orch = _make_orchestrator()
        result = await orch.execute("Hi", MockContextManager(), {}, {}, HookRegistry())
        assert "Error: No providers available" in result

    @pytest.mark.asyncio
    async def test_provider_exception_yields_error(self):
        """A generic exception from provider.complete() is yielded as error."""
        provider = FakeProvider()
        provider.complete = AsyncMock(side_effect=RuntimeError("connection lost"))
        provider.parse_tool_calls = MagicMock(return_value=[])

        orch = _make_orchestrator()
        result = await orch.execute(
            "Hi", MockContextManager(), _providers(provider), {}, HookRegistry()
        )
        assert "Error:" in result
        assert "connection lost" in result

    @pytest.mark.asyncio
    async def test_timeout_error_with_empty_message(self):
        """TimeoutError has empty str(); orchestrator handles it gracefully."""
        provider = FakeProvider()
        provider.complete = AsyncMock(side_effect=TimeoutError())
        provider.parse_tool_calls = MagicMock(return_value=[])

        orch = _make_orchestrator()
        result = await orch.execute(
            "Hi", MockContextManager(), _providers(provider), {}, HookRegistry()
        )
        assert "Error:" in result
        # Empty str(TimeoutError()) is handled -- should not be blank after "Error:"
        assert result.strip() != "Error:"

    @pytest.mark.asyncio
    async def test_max_iterations_injects_system_reminder(self):
        """When max_iterations is reached, the orchestrator injects a system
        reminder and makes one final provider call."""
        tc = ToolCall(id="tc_1", name="echo", arguments={})
        provider = _make_provider(
            responses=[
                # 1st: tool call (consumes the single allowed iteration)
                ChatResponse(content=[TextBlock(text="")], tool_calls=[tc]),
                # 2nd: the final "wrap up" call after max iterations
                ChatResponse(content=[TextBlock(text="Summary")]),
            ],
            tool_calls_seq=[[tc], []],
        )
        tools = {"echo": MockTool("echo", "result")}
        orch = _make_orchestrator(max_iterations=1)

        await orch.execute(
            "Do stuff",
            MockContextManager(),
            _providers(provider),
            tools,
            HookRegistry(),
        )

        # Verify two calls were made to provider.complete
        assert provider.complete.call_count == 2
        # The second call should contain the system reminder
        second_call_request = provider.complete.call_args_list[1][0][0]
        reminder_messages = [
            m
            for m in second_call_request.messages
            if isinstance(m.content, str) and "system-reminder" in m.content
        ]
        assert len(reminder_messages) == 1
        assert "maximum number of iterations" in reminder_messages[0].content


# ---------------------------------------------------------------------------
# TestCancellation -- graceful cancellation via coordinator
# ---------------------------------------------------------------------------


class TestCancellation:
    """Coordinator-driven cancellation at different points in the loop."""

    def _make_coordinator(self, cancelled=False):
        coord = MagicMock()
        coord.cancellation.is_cancelled = cancelled
        coord.cancellation.is_immediate = False
        coord.cancellation.register_tool_start = MagicMock()
        coord.cancellation.register_tool_complete = MagicMock()
        coord.process_hook_result = AsyncMock(return_value=MagicMock(action="continue"))
        return coord

    @pytest.mark.asyncio
    async def test_cancellation_at_iteration_start(self):
        """If cancelled before the first provider call, no output is produced."""
        provider = _make_provider(
            responses=[ChatResponse(content=[TextBlock(text="Nope")])],
            tool_calls_seq=[[]],
        )
        coord = self._make_coordinator(cancelled=True)
        orch = _make_orchestrator()

        result = await orch.execute(
            "Hi",
            MockContextManager(),
            _providers(provider),
            {},
            HookRegistry(),
            coordinator=coord,
        )
        # Provider should never be called -- cancelled at iteration start
        assert provider.complete.call_count == 0
        # Result is empty because no tokens were yielded
        assert result == ""

    @pytest.mark.asyncio
    async def test_cancellation_after_tools_adds_results_to_context(self):
        """Graceful cancellation after tool execution still adds tool results
        to context (prevents orphaned tool_calls)."""
        tc = ToolCall(id="tc_1", name="echo", arguments={})
        provider = _make_provider(
            responses=[
                ChatResponse(content=[TextBlock(text="")], tool_calls=[tc]),
            ],
            tool_calls_seq=[[tc]],
        )
        tools = {"echo": MockTool("echo", "result")}
        ctx = MockContextManager()
        coord = self._make_coordinator(cancelled=False)

        # Cancel AFTER the first provider call completes (during tool execution)
        original_execute = tools["echo"].execute

        async def cancel_during_tool(args):
            result = await original_execute(args)
            # Simulate cancellation happening while tool runs
            coord.cancellation.is_cancelled = True
            return result

        tools["echo"].execute = cancel_during_tool
        orch = _make_orchestrator()

        await orch.execute(
            "Go",
            ctx,
            _providers(provider),
            tools,
            HookRegistry(),
            coordinator=coord,
        )

        # Tool results MUST be in context despite cancellation
        messages = await ctx.get_messages_for_request()
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "tc_1"

    @pytest.mark.asyncio
    async def test_cancellation_status_emitted(self):
        """Cancelled execution emits 'cancelled' status in ORCHESTRATOR_COMPLETE."""
        provider = _make_provider(
            responses=[ChatResponse(content=[TextBlock(text="Nope")])],
            tool_calls_seq=[[]],
        )
        coord = self._make_coordinator(cancelled=True)
        hooks = HookRegistry()

        emitted_events = []

        async def capture_event(event_name, event_data):
            emitted_events.append(event_data)

        hooks.on("orchestrator:complete", capture_event)
        orch = _make_orchestrator()

        await orch.execute(
            "Hi",
            MockContextManager(),
            _providers(provider),
            {},
            hooks,
            coordinator=coord,
        )
        complete_events = [
            e for e in emitted_events if e.get("orchestrator") == "loop-interactive"
        ]
        assert len(complete_events) == 1
        assert complete_events[0]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# TestConstructor -- config defaults
# ---------------------------------------------------------------------------


class TestConstructor:
    """Constructor parses config with correct defaults."""

    def test_defaults(self):
        orch = InteractiveOrchestrator({})
        assert orch.max_iterations == -1
        assert orch.stream_delay == 0.01
        assert orch.extended_thinking is False
        assert orch.min_delay_between_calls_ms == 0

    def test_custom_config(self):
        orch = InteractiveOrchestrator(
            {
                "max_iterations": 5,
                "stream_delay": 0.05,
                "extended_thinking": True,
                "min_delay_between_calls_ms": 200,
            }
        )
        assert orch.max_iterations == 5
        assert orch.stream_delay == 0.05
        assert orch.extended_thinking is True
        assert orch.min_delay_between_calls_ms == 200
