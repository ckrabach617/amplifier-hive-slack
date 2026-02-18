# Proactive Director Alerts

**Status:** Planned
**Priority:** High -- Charlie may not remember what's in flight or check back
**Effort:** ~30 lines across 2 files (Phase 1), more for Phase 2

## Problem

The Director only communicates when Charlie sends a message. If a worker
fails, finishes, or hits a blocker, Charlie has no idea until she happens
to check in. She may not remember something is running. The Director needs
to be able to reach out proactively.

## Alert Types

| Event | Priority | Example |
|-------|----------|---------|
| **Worker failed** | Immediate | "Worker `shs-policy-update` failed: OCR tools not found" |
| **Worker completed** | Immediate | "Worker `shs-policy-update` finished -- results in SHS Documents/Drafts/" |
| **Worker needs input** | Immediate | "Worker `shs-policy-update` is blocked -- needs you to pick a template style" |
| **Long-running status** | Low | "Worker `big-research` still going -- 5 min in, making progress" |

## Design

Two independent paths that serve different purposes:

1. **Direct Slack post** (immediate awareness) -- posts to `#the-director`
   within seconds. No LLM, no lock contention, ~100ms. Formatted alert
   that tells Charlie what happened and what (if anything) she needs to do.
2. **Existing notify() queue** (Director context) -- unchanged. Director
   gets the full `[WORKER REPORT]` on Charlie's next message so it can
   discuss details intelligently.

Charlie sees the alert, decides if she cares right now, and can respond
whenever she wants. The Director will have full context when she does.

## Phase 1: Failure + Completion Alerts

### dispatch.py -- Add alert callback

Replace the single `on_failure` idea with a general `on_alert` callback:

```python
def __init__(
    self,
    session_manager,
    instance_name: str,
    working_dir: str,
    director_conversation_id: str = "",
    on_alert: Callable[[str, str, str], Awaitable[None]] | None = None,
) -> None:
    ...
    self._on_alert = on_alert  # (alert_type, task_id, message)
```

Call it on both failure AND success in `_run_worker`:

```python
# On success (after existing notify)
if self._on_alert:
    try:
        await self._on_alert("completed", task_id, summary)
    except Exception:
        logger.warning("Alert failed for %s", task_id, exc_info=True)

# On failure (after existing notify)
if self._on_alert:
    try:
        await self._on_alert("failed", task_id, str(e))
    except Exception:
        logger.warning("Alert failed for %s", task_id, exc_info=True)
```

### service.py -- Wire up the callback at tool construction

Create a closure where DispatchWorkerTool is constructed:

```python
async def _send_alert(alert_type: str, task_id: str, detail: str) -> None:
    detail_brief = detail[:200] + "..." if len(detail) > 200 else detail
    if alert_type == "failed":
        text = f"Worker `{task_id}` failed: {detail_brief}"
    elif alert_type == "completed":
        text = f"Worker `{task_id}` finished. {detail_brief}"
    elif alert_type == "blocked":
        text = f"Worker `{task_id}` needs your input: {detail_brief}"
    else:
        text = f"Worker `{task_id}`: {detail_brief}"
    await slack_client.chat_postMessage(
        channel=notify_channel,
        text=text,
        username=persona_name,
        icon_emoji=persona_emoji,
    )

# Pass to tool
DispatchWorkerTool(..., on_alert=_send_alert)
```

## Phase 2: Worker-Initiated Alerts (Blocked/Needs Input)

Workers currently can't signal "I'm stuck, need Charlie." This requires
giving workers a way to call `on_alert("blocked", task_id, question)`.

Options (pick one when implementing):
- **Tool approach:** Give workers a `signal_blocker` tool that calls the
  alert callback. Worker says "I need Charlie to pick a template" and the
  tool fires the alert + updates TASKS.md to "Waiting on Charlie."
- **Convention approach:** Worker writes "BLOCKED: ..." to TASKS.md. A
  periodic check (or post-execution hook) detects it and fires the alert.

Tool approach is cleaner -- the worker explicitly signals, no polling.

## Phase 3: Long-Running Status (Optional)

For workers running >5 minutes, post a periodic status update:
"Worker `big-research` still running -- 5 min in."

This would be a timer in `_run_worker` that fires every N minutes.
Low priority -- Phase 1+2 cover the important cases.

## Design Decisions

- **Direct Slack, not through LLM** -- no lock contention, no cost, instant
- **dispatch.py stays Slack-agnostic** -- opaque async callback
- **Best-effort** -- if Slack post fails, notify() queue is the safety net
- **Persona preserved** -- uses Director's name/emoji in `#the-director`
- **Both paths coexist** -- alert gives awareness, notify() queue gives
  Director context for when Charlie responds
- **No threading** -- matches `#the-director`'s `threads:off` mode
