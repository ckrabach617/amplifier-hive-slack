# Injectable Orchestrator Design

## The Problem

When a user sends a message to an Amplifier instance via Slack, the orchestrator runs to completion before the next message can be processed. This creates two UX problems:

1. **No feedback** — The user sees nothing during long operations (30s to several minutes)
2. **No mid-execution input** — If the user sends follow-up messages ("also check tests", "actually focus on auth"), they queue up and become separate executions instead of steering the current work

## The Solution: `loop-interactive`

A new orchestrator module that extends `loop-streaming` with two capabilities:

1. **Message injection** — External callers can push messages into the running loop, which get picked up at the next natural boundary (after tool results, before the next LLM call)
2. **Progress callbacks** — External callers receive real-time events as the loop progresses (tool starts, tool finishes, LLM thinking, etc.)

## How the Current Orchestrator Works

The existing `loop-streaming` orchestrator (from deep source analysis, ~1100 lines):

```
execute(prompt):
  1. prompt:submit hook
  2. execution:start event
  3. Add user message to context
  4. Select provider

  WHILE not done:
    A. Check cancellation
    B. iteration += 1
    C. provider:request hook (can inject ephemeral context)
    D. Get messages for request (context handles compaction)
    E. Apply ephemeral injections from hooks
    F. Apply pending tool:post injections from previous iteration
    G. Build ChatRequest
    H. Rate limit delay (if configured)
    I. Call provider (LLM) — streaming or non-streaming path
    J. Parse response

    K. If NO tool calls:
       Stream response text, add to context, BREAK

    L. If tool calls:
       Execute ALL tools in parallel (asyncio.gather)
       For each: tool:pre hook → tool.execute() → tool:post hook
       Add tool results to context
       CONTINUE (loop back)

  5. execution:end event
  6. orchestrator:complete event
```

Loop terminates when: no tool calls in response, cancellation, provider error, max iterations, or hook denial.

## The New Loop: 3 Injection Points

```
WHILE not done:
    A. Check cancellation
    B. iteration += 1

    >>> INJECTION POINT 1: Top of iteration <<<
    Check queue, add any user messages to context
    (LLM sees them on the NEXT provider call)

    C-I. [existing: hooks, context, build request, call provider]
    J. Parse response

    K. If NO tool calls:
       >>> INJECTION POINT 2: Before committing to "done" <<<
       If messages queued → add to context, CONTINUE (don't break!)
       If empty → stream text, BREAK (normal exit)

    L. If tool calls:
       Execute tools, add results to context
       >>> INJECTION POINT 3: After tools, before continue <<<
       Check queue, add any messages (next LLM call sees them)
       CONTINUE
```

**Why 3 points:** Point 1 catches messages queued between iterations. Point 2 prevents premature exit — if the LLM was about to finish but the user just sent more input, we loop back instead of returning. Point 3 catches messages sent during tool execution (which can take seconds).

## The Injection Queue

```python
class InteractiveOrchestrator:
    def __init__(self, config):
        self._injection_queue: asyncio.Queue[str] = asyncio.Queue()

    def inject_message(self, content: str) -> None:
        """Inject a user message into the running execution loop.

        Called from external async tasks (e.g., Slack connector).
        Thread-safe via asyncio.Queue. The message is picked up at
        the next injection point in the loop.
        """
        self._injection_queue.put_nowait(content)
```

Registered as a coordinator capability so the connector can access it:

```python
def mount(coordinator, config):
    orchestrator = InteractiveOrchestrator(config)
    coordinator.mount("orchestrator", orchestrator)
    coordinator.register_capability(
        "orchestrator.inject_message",
        orchestrator.inject_message,
    )
```

The connector injects via:
```python
inject_fn = session.coordinator.get_capability("orchestrator.inject_message")
if inject_fn:
    inject_fn("Also check the test coverage")
```

## How Injected Messages Appear in Context

Multiple queued messages are combined into one context addition:

```python
def _drain_injection_queue(self, context, hooks):
    """Check the queue and add any pending messages to context."""
    messages = []
    while not self._injection_queue.empty():
        try:
            messages.append(self._injection_queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    if not messages:
        return False

    combined = "\n".join(f"- {m}" for m in messages)
    injection = (
        "[The user sent additional messages while you were working. "
        "Incorporate this into your current task:]\n"
        f"{combined}"
    )
    context.add_message({"role": "user", "content": injection})

    if hooks:
        hooks.emit("injection:applied", {
            "messages": messages,
            "count": len(messages),
        })

    return True
```

The LLM sees this as a user message and naturally incorporates it into its reasoning. The `[bracketed prefix]` gives context about WHY it appeared mid-conversation.

## Progress Callback

The orchestrator accepts an optional callback for real-time progress:

```python
async def execute(
    self, prompt, context, providers, tools, hooks,
    coordinator=None,
    on_progress=None,  # Callable[[str, dict], Awaitable[None]] | None
):
```

Events fired:

| Event | When | Data |
|-------|------|------|
| `executing` | Start of execution | `{"prompt": "..."}` |
| `thinking` | Before each LLM call | `{"iteration": N}` |
| `tool:start` | Before each tool | `{"tool": "read_file", "args": {...}}` |
| `tool:end` | After each tool | `{"tool": "read_file", "duration": 1.2}` |
| `injection:applied` | Queue drained | `{"count": 2, "messages": [...]}` |
| `complete` | Done | `{"iterations": N, "status": "success"}` |

## The Slack User Experience

### Normal request:
```
User:     Analyze this codebase and suggest improvements
  ⏳ (instant reaction)

Bot:      ⚙️ Working...
Bot:      ⚙️ Reading files... (3 tools active)
Bot:      ⚙️ Analyzing auth module (8 files read)

  (status message deleted)
Alpha:    Here's my analysis...
  (⏳ removed)
```

### With mid-execution steering:
```
User:     Analyze this codebase
  ⏳ (instant)
Bot:      ⚙️ Reading files...

User:     Also check the test coverage
  📨 (instant — injected into running execution)
Bot:      ⚙️ Incorporating your input... Reading test files

User:     And focus especially on auth
  📨 (injected)
Bot:      ⚙️ Incorporating your input... Focusing on auth module

  (status message deleted)
Alpha:    Here's my analysis, including test coverage with a
          focus on the auth module...
  (⏳ removed)
```

### How the connector uses it:

```python
async def _handle_message(self, event, say):
    # ... routing ...

    # Check if this conversation is already executing
    if conversation_id in self._active_executions:
        # INJECT into running execution instead of queuing
        session = self._get_active_session(conversation_id)
        inject_fn = session.coordinator.get_capability("orchestrator.inject_message")
        if inject_fn:
            inject_fn(prompt)
            # Acknowledge with 📨
            await self._app.client.reactions_add(
                channel=channel, timestamp=event["ts"],
                name="incoming_envelope",
            )
            return

    # Normal path: start new execution
    self._active_executions.add(conversation_id)

    # ⏳ on user's message
    await self._app.client.reactions_add(
        channel=channel, timestamp=event["ts"],
        name="hourglass_flowing_sand",
    )

    # Editable status message (bot's own identity — NOT persona)
    status_msg = await self._app.client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text="⚙️ Working...",
    )

    # Progress callback updates the status message
    last_update = [0.0]  # Throttle to every 2 seconds

    async def on_progress(event_type, data):
        import time
        now = time.time()
        if now - last_update[0] < 2.0:
            return
        last_update[0] = now

        if event_type == "tool:start":
            text = f"⚙️ {_friendly_tool_name(data.get('tool', ''))}..."
        elif event_type == "thinking":
            text = f"⚙️ Thinking... (iteration {data.get('iteration', '?')})"
        elif event_type == "injection:applied":
            text = "⚙️ Incorporating your input..."
        else:
            return

        try:
            await self._app.client.chat_update(
                channel=channel, ts=status_msg["ts"], text=text,
            )
        except Exception:
            pass  # Best effort

    try:
        response = await self._service.execute(
            instance_name, conversation_id, prompt,
            on_progress=on_progress,
        )

        # Delete status message
        await self._app.client.chat_delete(
            channel=channel, ts=status_msg["ts"],
        )

        # Post final response with persona
        await say(
            text=markdown_to_slack(response),
            thread_ts=thread_ts,
            username=instance.persona.name,
            icon_emoji=instance.persona.emoji,
        )

        # Remove ⏳
        await self._app.client.reactions_remove(
            channel=channel, timestamp=event["ts"],
            name="hourglass_flowing_sand",
        )
    finally:
        self._active_executions.discard(conversation_id)
```

### Status message constraint

Messages posted with `chat:write.customize` (custom username/avatar) CANNOT be edited via `chat.update`. This is a Slack API limitation. So:

- **Status message** → posted as bot's own identity (editable)
- **Final response** → posted with persona (Alpha/Beta name + emoji)
- **Status message deleted** before final response posted (clean transition)

## Module Structure

```
amplifier-module-loop-interactive/
├── pyproject.toml
├── amplifier_module_loop_interactive/
│   ├── __init__.py          # mount(), capability registration
│   └── orchestrator.py      # InteractiveOrchestrator (fork of loop-streaming)
└── tests/
    ├── test_injection.py    # Queue injection at each point
    └── test_progress.py     # Progress callback tests
```

Fork of `loop-streaming` (~1100 lines) with additions:
- `_injection_queue: asyncio.Queue[str]` (~5 lines)
- `inject_message(content)` method (~5 lines)
- `_drain_injection_queue(context, hooks)` (~20 lines)
- 3 injection point checks in the main loop (~15 lines)
- `on_progress` callback wiring (~20 lines)
- Coordinator capability registration (~5 lines)
- New event: `injection:applied` (~3 lines)

**~70 lines of new code** on top of the ~1100 line fork.

## Implementation Phases

### Phase 1: Progress indicators with existing orchestrator (ship first)

Use the EXISTING `loop-streaming` — no new orchestrator needed yet:
- Register hooks for `tool:pre`, `tool:post` events on the session
- Hooks call back to the connector to update the status message
- ⏳ reaction on receipt, status message lifecycle (create → update → delete)
- Queue messages locally (batch after execute, not inject)

This gives immediate feedback while we build the orchestrator.

**Changes:** ~150 lines in `slack.py` + `service.py`

### Phase 2: `loop-interactive` orchestrator module

Fork `loop-streaming`, add injection queue + progress callback:
- New repo: `amplifier-module-loop-interactive`
- ~70 lines added to the fork
- Bundle config change: `orchestrator.module: loop-interactive`

**Changes:** New module (~1200 lines, mostly forked)

### Phase 3: Connector injection integration

Wire the connector to use injection instead of local queuing:
- `_active_executions` tracking per conversation
- `inject_message()` via coordinator capability
- 📨 reaction on injected messages
- Status message shows "Incorporating your input..."

**Changes:** ~100 lines in `slack.py`

### Phase 4: Cancellation via reaction

❌ reaction on status message triggers `cancel:requested`:
- The orchestrator already checks cancellation at loop top
- Wire reaction handler to set the cancellation token

**Changes:** ~30 lines in `slack.py`

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Injection mechanism | `asyncio.Queue` + coordinator capability | Thread-safe, decoupled |
| Injected message role | `user` with prefix | LLM understands naturally |
| Multiple queued messages | Combined into one | One context add, not N |
| 3 injection points | Top of loop + before break + after tools | Maximum responsiveness |
| Status message identity | Bot's own (not persona) | Must be editable |
| Final response identity | Persona (custom username) | Consistent UX |
| New module vs modify | New module (`loop-interactive`) | Doesn't break existing |
| Progress mechanism | Callback on execute() | Simple, optional, backward compatible |

## Force-Respond Mechanism

Some tools (like `dispatch_worker`) need the Director to respond to the user
immediately after the tool runs, rather than continuing to call more tools.
Prompt instructions ("respond after dispatching") are unreliable -- the LLM
ignores them. Force-respond solves this mechanically.

### How It Works

After tool results are processed, the orchestrator checks if any executed tool
is in the `force_respond_tools` set. If so, it sets a one-shot flag. On the
next iteration, `tools_list` is set to `None` -- the LLM literally has no
tools available, so it MUST produce text.

```python
# In the loop body, after tool results are added to context:
if any(tn in self._force_respond_tools for _, tn, _ in tool_results):
    _force_respond = True

# On the next iteration, when building ChatRequest:
if _force_respond:
    _force_respond = False  # one-shot reset
    tools_list = None  # LLM must respond with text
```

### Configuration

`force_respond_tools` is configurable via the orchestrator config dict,
defaulting to `["dispatch_worker"]`:

```python
# In InteractiveOrchestrator.__init__:
self._force_respond_tools: set[str] = set(
    config.get("force_respond_tools", ["dispatch_worker"])
)

# In service.py orchestrator overlay:
"config": {
    "extended_thinking": True,
    "force_respond_tools": ["dispatch_worker", "recipes"],
}
```

Adding a new force-respond tool is a one-line config change in `service.py`.

### Interaction with Injection Queue

Worker completion reports use `notify()` (queued for next `execute()` call),
NOT `inject_message()` (mid-execution). This is deliberate -- injecting worker
reports mid-execution can hijack the force-respond cycle (triggering injection
point 2 after a force-respond, causing the Director to loop instead of posting
its response). See `service.py:528-542` for the documented rationale.

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Force mechanism | Strip tools from LLM call | Mechanical enforcement, not prompt-dependent |
| Scope | One-shot (resets after one call) | Tools available again if loop continues |
| Configuration | `force_respond_tools` in config dict | Adding tools doesn't require orchestrator edits |
| Worker reports | `notify()` not `inject_message()` | Prevents force-respond cycle hijacking |

## SessionManager Protocol Change

```python
class SessionManager(Protocol):
    async def execute(
        self,
        instance_name: str,
        conversation_id: str,
        prompt: str,
        on_progress: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> str: ...
```

Optional `on_progress` parameter with `None` default — fully backward compatible. The `InProcessSessionManager` registers the callback as a hook on the session, translating orchestrator events into progress calls. The future `GrpcSessionManager` maps progress to server-streaming gRPC responses.

## Relationship to Architecture

- The `orchestrator.inject_message` capability follows Amplifier's coordinator capability pattern (same as `session.spawn`, `session.resume`)
- When the Rust service exists, injection maps to gRPC bidirectional streaming
- When the Rust core exists, the `asyncio.Queue` becomes a `tokio::sync::mpsc` channel
- The progress callback maps to the gRPC `ExecuteStream` response stream
- The `.proto` already designed for this: `SessionService.ExecuteStream` returns `stream ExecutionEvent`
