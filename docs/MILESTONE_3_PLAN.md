# Milestone 3: Connector-Provided Actions + Kernel Integration

## Summary

Give the Amplifier instance the ability to ACT in Slack — not just respond to messages, but react with emoji, post to other channels, ask the user for approval with interactive buttons, and surface status messages from hooks.

**Approach:** Build directly in the connector. No new repos, no new bundles, no platform abstraction. Wire the existing Amplifier kernel protocols (`ApprovalSystem`, `DisplaySystem`) and mount connector-provided tools on sessions post-creation.

**Scope:** 3 new files + 2 modified files. ~400 lines of new code. Zero new packages.

---

## Why Not the Two-Bundle Pattern?

The original plan called for `amplifier-bundle-hive` (base) + `amplifier-bundle-hive-slack` (extension), following the LSP pattern. After analysis, this is premature:

- The LSP pattern works because all language servers speak the same protocol. Chat platforms (Slack, Discord, Teams) have completely different APIs — there's no shared abstraction.
- We have ONE platform. An abstraction designed for one consumer will be wrong when the second arrives.
- The Amplifier kernel already has `ApprovalSystem` and `DisplaySystem` protocols that we're passing `None` for — zero new infrastructure needed.
- `coordinator.mount("tools", tool)` works post-session-creation — we can inject tools with live Slack client access directly.

When a second platform arrives, THAT's when we'll understand the real boundary and can extract.

---

## What Gets Built

### 3.1: SlackDisplaySystem

**File:** `src/hive_slack/display.py` (NEW, ~30 lines)

Implements the `DisplaySystem` protocol from amplifier-core. When a hook sets `user_message` (status updates, warnings, info), post it to the Slack channel instead of just logging.

```python
class SlackDisplaySystem:
    """Route hook display messages to Slack."""

    def __init__(self, slack_client, channel: str, thread_ts: str = ""):
        self._client = slack_client
        self._channel = channel
        self._thread_ts = thread_ts

    def show_message(self, message: str, level: str = "info", source: str = "hook"):
        """Post a message to the Slack channel."""
        prefix = {"warning": "⚠️ ", "error": "🚨 "}.get(level, "")
        # Fire-and-forget (hooks shouldn't block)
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._post(f"{prefix}{message}"))
        except RuntimeError:
            pass  # No event loop — log only

    async def _post(self, text: str):
        try:
            await self._client.chat_postMessage(
                channel=self._channel,
                thread_ts=self._thread_ts,
                text=text,
            )
        except Exception:
            pass  # Best effort
```

**Wired in `service.py`:**
```python
session = await prepared.create_session(
    session_cwd=working_dir,
    display_system=SlackDisplaySystem(slack_client, channel, thread_ts),
)
```

**What the user sees:** Hook status messages appear as real Slack messages in the thread instead of being silently logged.

### 3.2: SlackApprovalSystem

**File:** `src/hive_slack/approval.py` (NEW, ~120 lines)

Implements the `ApprovalSystem` protocol from amplifier-core. When a hook returns `ask_user` (e.g., tool confirmation, destructive operation guard), post Block Kit buttons in Slack and wait for the user's click.

```
Amplifier:  I'd like to delete 3 temporary files. Allow?
            [Allow]  [Deny]

User clicks: [Allow]

Amplifier:  Done, files deleted.
```

**Key design:**
- Posts a Block Kit message with buttons using the `blocks` parameter
- Each button has a unique `action_id` containing a correlation ID
- Uses `asyncio.Event` to wait for the Slack `block_actions` interaction event
- Respects the `timeout` parameter from the kernel — falls back to `default` if no response
- Cleans up the button message after resolution (edits to show the choice)

**Interaction handler:** The connector (`slack.py`) registers a `block_actions` handler that resolves pending approvals by correlation ID.

```python
# In SlackConnector.__init__:
self._pending_approvals: dict[str, asyncio.Event] = {}
self._approval_results: dict[str, str] = {}
self._app.action(re.compile(r"^approval_.*"))(self._handle_approval_action)
```

**What the user sees:** Interactive Yes/No (or multi-option) buttons in the thread. Clicking a button continues execution. If they don't click within the timeout, the default action applies.

### 3.3: Connector-Provided Slack Tools

**File:** `src/hive_slack/tools.py` (NEW, ~120 lines)

Two tools, mounted on each session post-creation:

#### `slack_send_message`

Post a message to a Slack channel or thread.

```json
{
  "name": "slack_send_message",
  "description": "Send a message in Slack. Use to post updates, notifications, or results to a channel.",
  "parameters": {
    "text": "The message text (markdown supported)",
    "channel": "Channel name or ID (optional — defaults to current channel)",
    "thread_ts": "Thread timestamp to reply to (optional — defaults to current thread)"
  }
}
```

Use cases:
- Instance posts a summary to a different channel
- Instance notifies the user in a DM while working in a channel
- Instance posts a status update in the current thread

#### `slack_add_reaction`

Add an emoji reaction to a message.

```json
{
  "name": "slack_add_reaction",
  "description": "Add an emoji reaction to a message. Use to acknowledge, signal status, or mark messages.",
  "parameters": {
    "emoji": "Emoji name without colons (e.g., 'thumbsup', 'white_check_mark', 'eyes')",
    "message_ts": "Message timestamp to react to (optional — defaults to the user's last message)"
  }
}
```

Use cases:
- React with 👀 when starting to look at something
- React with ✅ when a task is complete
- React with ⚠️ when something needs attention

#### Tool Mounting

```python
# In service.py — _get_or_create_session(), after create_session():

slack_tools = create_slack_tools(
    slack_client=slack_client,
    channel=channel,
    thread_ts=thread_ts,
    instance_name=instance_name,
)
for tool in slack_tools:
    await session.coordinator.mount("tools", tool)
```

### 3.4: Wiring Changes

**`service.py`** — Modified:
- `_get_or_create_session()` gains optional `slack_client`, `channel`, `thread_ts` parameters
- Creates `SlackApprovalSystem` and `SlackDisplaySystem` from the client
- Passes them to `create_session()`
- Mounts Slack tools post-creation
- Falls back gracefully when no Slack client provided (for testing, CLI use)

**`slack.py`** — Modified:
- `_execute_with_progress()` passes `self._app.client`, `channel`, `thread_ts` to service
- Registers `block_actions` handler for approval button clicks
- Manages `_pending_approvals` dict for correlation

---

## Architecture

```
SlackConnector (owns AsyncApp + WebClient)
    │
    ├── Passes slack_client to SessionManager
    │
    ├── Handles block_actions events
    │     └── Resolves pending approvals by correlation ID
    │
    └── SessionManager._get_or_create_session()
          │
          ├── SlackApprovalSystem ←── kernel "ask_user" results
          │     └── Block Kit buttons + asyncio.Event wait
          │
          ├── SlackDisplaySystem  ←── kernel "user_message" results
          │     └── Post to channel/thread
          │
          └── Connector Tools     ←── mounted on coordinator
                ├── slack_send_message
                └── slack_add_reaction

    All share the same authenticated WebClient instance.
    No new repos, packages, or bundles.
```

---

## Implementation Order

### Step 1: SlackDisplaySystem (simplest, immediate value)

- Create `src/hive_slack/display.py` (~30 lines)
- Implements `DisplaySystem` Protocol (just `show_message()`)
- Wire into `service.py` `_get_or_create_session()`
- Tests: display posts to channel, handles errors gracefully, formats warning/error prefixes

### Step 2: SlackApprovalSystem (highest value)

- Create `src/hive_slack/approval.py` (~120 lines)
- Implements `ApprovalSystem` Protocol (`request_approval()`)
- Block Kit message with action buttons
- `asyncio.Event` waiting for user response
- Timeout handling with configurable default
- Wire into `service.py`
- Register `block_actions` handler in `slack.py`
- Tests: approval flow, timeout/default, button resolution, cleanup

### Step 3: Connector tools

- Create `src/hive_slack/tools.py` (~120 lines)
- `SlackSendMessageTool` and `SlackReactionTool`
- `create_slack_tools()` factory function
- Mount in `service.py` post-session-creation
- Tests: tool schemas, execute with mock client, error handling

### Step 4: Wiring + integration tests

- Modify `service.py` to accept and pass Slack client through
- Modify `slack.py` to provide client and handle approval interactions
- Integration tests: full flow from message → tool use → Slack API call

---

## What the Early Adopter Gets

After this milestone, their assistant can:

| Capability | Example |
|------------|---------|
| **Ask for confirmation** | "Should I overwrite budget.xlsx?" → [Yes] [No] buttons |
| **Give choices** | "Which format?" → [PDF] [DOCX] [HTML] buttons |
| **React to messages** | 👀 when starting work, ✅ when done, ⚠️ on issues |
| **Post to other channels** | "I've posted the summary to #reports" |
| **Surface hook messages** | "⚠️ Large file detected" appears in the thread |

---

## What This Does NOT Include (Deferred)

| Feature | Why Deferred |
|---------|-------------|
| `create_channel` | Not useful for single-instance |
| `invite_to_channel` | Not useful for single-instance |
| `list_channels` / `list_members` | Not useful yet |
| Instance-to-instance DMs | Zero demand |
| Two-bundle architecture | Only one platform |
| Block Kit rich formatting (tables, collapsible) | Nice-to-have |
| Proactive notifications | Separate feature (file watching) |
| Pin messages | Low priority |

These become trivial additions later — `create_channel` is just another tool in `tools.py` when the time comes.

---

## Scopes Needed

No new Slack scopes needed. Everything uses existing scopes:
- `chat:write` — send messages, post Block Kit (already have)
- `reactions:write` — add reactions (already have)
- `chat:write.customize` — persona posting (already have)

The interactivity handling (Block Kit button clicks) is already enabled in our manifest (`interactivity.is_enabled: true`).

---

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| Block Kit interaction events via Socket Mode | Socket Mode handles `interactive` events; `slack-bolt` has `app.action()` handler |
| Approval timeout blocking the session | `asyncio.wait_for()` with kernel-provided timeout; default action on timeout |
| Tool injection confusing the orchestrator | Verified: `coordinator.mount("tools", ...)` adds to the same dict the orchestrator reads |
| Slack client not available in test context | All Slack dependencies are optional; service works without them (for CLI/testing) |

---

## Test Strategy

- Unit tests for each new class (display, approval, tools) with mocked Slack client
- Integration tests for the wiring (service creates session with systems + tools)
- Existing 124 tests continue to pass (no regression)
- Target: ~20 new tests
