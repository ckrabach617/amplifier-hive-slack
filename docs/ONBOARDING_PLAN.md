# Early Adopter Onboarding Plan

## Summary

A lightweight onboarding system that teaches new users how to work with the assistant through progressive, one-time messages. Scaffold that dissolves — after ~6 interactions, it goes silent forever.

**Scope:** 1 new file (`onboarding.py`, ~130 lines), ~50 lines added to `slack.py`, 1 manifest change.

---

## Architecture

```
New file:   hive_slack/onboarding.py   (~130 lines)
Modified:   hive_slack/slack.py        (~50 lines added)
State:      ~/.amplifier/hive/users/{user_id}/onboarding.json
Manifest:   Add im:write scope (for proactive DMs)
```

---

## Per-User State

```
~/.amplifier/hive/users/{user_id}/onboarding.json
```

```json
{
  "version": 1,
  "user_id": "U789XYZ",
  "first_seen": "2026-02-08T22:15:00Z",
  "welcomed": true,
  "threads_started": 5,
  "recent_threads": ["C123:1707432900.123456", "dm:U789XYZ"],
  "tips_shown": {
    "regenerate": "2026-02-09T10:00:00Z",
    "file_upload": null,
    "mid_execution": null
  },
  "cross_thread_notes_shown": 1
}
```

Flat JSON file per user. Survives restarts. Separate from session transcripts (different lifecycle — per-user vs per-conversation).

---

## The 5 Onboarding Pieces

### 1. Welcome DM

**Trigger:** First-ever message from a user to the bot (any path — DM, @mention, channel). Detected by `onboarding.is_first_interaction` (checks `welcomed == false`).

**Sent:** Via `conversations.open` + `chat.postMessage` as a DM, before execution starts. Adds ~200ms to the first interaction only.

**Message:**

```
Hey — I'm {persona_name}. Since this is your first time, one thing worth knowing:

Each thread is its own conversation. I start fresh every time, so I won't have
context from other threads. If you need to reference something from elsewhere,
just paste the relevant bit.

You can @mention me in channels or message me here directly.
```

### 2. Thread Footer (first 3 threads)

**Trigger:** First response in a new thread, for the user's first 3 threads. Appended to the bot's response.

**Message:**

```
---
_New thread, fresh start — I don't have context from your other conversations._
```

Dropped after 3 threads. Short, positively framed ("fresh start"), avoids jargon ("session", "thread").

### 3. Progressive Feature Tips (shown once each)

One tip per response. Never during the footer phase (threads 1-3). Each shown exactly once.

**Priority order:** cross-thread note > thread footer > mid-execution tip > regenerate tip > file upload tip

#### Tip A: Regenerate Reaction
**Trigger:** First new thread after footer phase ends (thread 4+).
```
---
_Tip: React with :arrows_counterclockwise: on any of my responses to get a fresh take._
```

#### Tip B: File Upload
**Trigger:** Next new thread after regenerate tip shown.
```
---
_Tip: You can drop files into the thread — code, images, docs. I'll read them._
```

#### Tip C: Mid-Execution Messaging
**Trigger:** First response that takes >20 seconds, after footer phase.
```
---
_Tip: When you see the :hourglass_flowing_sand:, you can send follow-up messages to steer what I'm doing._
```

### 4. Cross-Thread Confusion Handler

**Trigger:** New thread + text matches backward-reference patterns + shown fewer than 3 times lifetime.

**Detection patterns:** "as I mentioned", "remember when", "you said", "from earlier", "continuing from", "pick up where", etc. (regex-based, multi-word phrases to minimize false positives).

**Message:**
```
---
_Heads up: each thread is its own conversation, so I don't have context from
other threads. If you're referring to something specific, paste it here and
I'll pick right up._
```

Capped at 3 lifetime showings. After that, the user understands.

### 5. State Tracking (`onboarding.py`)

```python
class UserOnboarding:
    @classmethod
    async def load(cls, user_id: str) -> "UserOnboarding": ...

    @property
    def is_first_interaction(self) -> bool: ...

    def mark_welcomed(self) -> None: ...
    def record_thread(self, conversation_id: str) -> bool: ...  # Returns is_new_thread

    @staticmethod
    def has_cross_thread_reference(text: str) -> bool: ...

    def get_response_suffix(
        self, is_new_thread: bool, response_duration: float,
        has_cross_thread_ref: bool,
    ) -> str: ...

    async def save(self) -> None: ...
```

`get_response_suffix()` handles all priority logic internally. Returns the appropriate suffix or empty string. Only one suffix per response.

---

## Integration into slack.py

In both `_handle_mention` and `_handle_message`, after resolving user and instance:

```python
onboarding = await UserOnboarding.load(user)
if onboarding.is_first_interaction:
    await self._send_welcome_dm(user, instance.persona)
    onboarding.mark_welcomed()

is_new_thread = onboarding.record_thread(conversation_id)
has_cross_ref = (
    UserOnboarding.has_cross_thread_reference(text)
    if is_new_thread else False
)
```

Pass `onboarding`, `is_new_thread`, `has_cross_ref` to `_execute_with_progress()`.

In `_execute_with_progress()`, after execution completes, before `say()`:

```python
duration = time.monotonic() - start_time
text = markdown_to_slack(response)
if onboarding:
    suffix = onboarding.get_response_suffix(is_new_thread, duration, has_cross_ref)
    if suffix:
        text = f"{text}\n{suffix}"
    asyncio.create_task(onboarding.save())  # fire-and-forget
```

---

## Manifest Change

Add `im:write` to bot token scopes (required for `conversations.open` to initiate DMs proactively). One line in `slack-manifest.yaml`, requires Slack app reinstall.

---

## What NOT to Build

| Skip | Why |
|------|-----|
| `team_join` event handler | First-interaction trigger covers it |
| Block Kit rich cards for onboarding | Plain text is warmer than a formatted card |
| `/help` slash command | Progressive tips beat a menu nobody reads |
| Interactive tutorial wizard | 4 messages + 3 tips IS the tutorial |
| "Turn off tips" preference | Each tip shows once. Nothing to turn off. |
| Per-channel customization | Same user, same bot, same concepts |
| Onboarding analytics | One early adopter. Just ask them. |
| Tip content in the LLM system prompt | Connector owns this — deterministic, not probabilistic |

---

## Implementation Order

```
Phase 1:  onboarding.py (state + messages + suffix logic)   ~130 lines
Phase 2:  slack.py integration (welcome DM + suffix wiring)  ~50 lines
Phase 3:  Manifest update (add im:write)                     1 line
```

Total: ~180 lines of code. The entire system is ~6 interactions deep, then silent forever.
