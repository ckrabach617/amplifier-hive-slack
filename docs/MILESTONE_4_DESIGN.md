# Milestone 4 — Multi-Instance Communication Patterns

## Complete Design Specification

**Status:** Design complete, ready for implementation
**Depends on:** Milestones 0–3 (all DONE)
**Estimated total effort:** ~600 lines of production code, ~500 lines of tests

---

## Table of Contents

1. [Feature 1: Thread Ownership](#feature-1-thread-ownership) — MUST-HAVE
2. [Feature 2: Open Floor / Roundtable Mode](#feature-2-open-floor--roundtable-mode) — MUST-HAVE
3. [Feature 3: Emoji Summoning](#feature-3-emoji-summoning) — MUST-HAVE
4. [Feature 4: Streaming Response](#feature-4-streaming-response) — NICE-TO-HAVE
5. [Feature 5: Cross-Machine Coordination](#feature-5-cross-machine-coordination) — DEFER
6. [Implementation Order](#implementation-order)
7. [Shared Infrastructure](#shared-infrastructure)

---

## Feature 1: Thread Ownership

**Priority: MUST-HAVE** — Foundation for roundtable and all multi-instance thread behavior.

### 1.1 Recommended Approach

Track which instance "owns" each thread in an in-memory bounded dict. Ownership is
established by the first instance to respond. Follow-up messages in that thread
auto-route to the owner without any prefix. Explicit addressing (`@beta`, `beta:`)
overrides and transfers ownership.

**Why in-memory, not persistent:**
- Thread ownership is ephemeral — conversations have natural lifespans
- On bot restart, fallback to channel routing is perfectly fine (user just prefixes once)
- No database dependency, no cleanup jobs, no migration path
- Bounded to 10,000 entries with oldest-eviction — covers weeks of activity

**Key decision: Ownership transfer on explicit addressing.**
When a user says `@beta what do you think?` in an alpha-owned thread, beta responds
AND becomes the new owner. This matches user intent — if they explicitly called beta,
they probably want to keep talking to beta. If they want alpha back, they say `@alpha`.

### 1.2 UX — What the User Sees

```
#coding channel (topic: [default:alpha])

User:     Review this PR                          ← no prefix, uses channel default
Alpha:    Looking at it, I see several issues...   ← Alpha responds, OWNS this thread

  (later, same thread)
User:     What about the error handling?           ← no prefix
Alpha:    Good point, the try/except on line 42... ← auto-routed to Alpha (thread owner)

User:     @beta What's your take?                  ← explicit override
Beta:     I'd approach the error handling...       ← Beta responds, TAKES ownership

User:     And what about logging?                  ← no prefix
Beta:     For logging, I'd suggest...              ← auto-routed to Beta (new owner)
```

**DM threads:** Ownership is implicit — DMs have a single instance (the default).
No tracking needed.

**@mention in unowned thread:** If someone @mentions the bot in a thread that has
no owner yet (e.g., a thread started by another human), the mentioned/addressed
instance gets ownership.

### 1.3 Implementation Plan

**State storage — `slack.py` `__init__`:**

```python
# Thread ownership: conversation_id → instance_name
# Bounded to 10,000 entries, oldest evicted on overflow
self._thread_owners: dict[str, str] = {}
self._thread_owner_order: list[str] = []  # insertion order for eviction
_THREAD_OWNER_LIMIT = 10_000
```

**Record ownership — `slack.py` `_execute_with_progress()`:**

After the `say()` call that posts the final response (line ~494), add:

```python
self._set_thread_owner(conversation_id, instance_name)
```

**New method — `_set_thread_owner()`** (~15 lines):

```python
def _set_thread_owner(self, conversation_id: str, instance_name: str) -> None:
    """Record or transfer thread ownership."""
    if conversation_id in self._thread_owners:
        # Transfer: remove from order list, will re-add at end
        try:
            self._thread_owner_order.remove(conversation_id)
        except ValueError:
            pass
    self._thread_owners[conversation_id] = instance_name
    self._thread_owner_order.append(conversation_id)
    # Evict oldest if over limit
    while len(self._thread_owners) > self._THREAD_OWNER_LIMIT:
        oldest = self._thread_owner_order.pop(0)
        self._thread_owners.pop(oldest, None)
```

**New method — `_get_thread_owner()`** (~5 lines):

```python
def _get_thread_owner(self, conversation_id: str) -> str | None:
    """Get the instance that owns this thread, or None."""
    return self._thread_owners.get(conversation_id)
```

**Modify routing in `_handle_message()`** — insert BEFORE the channel config routing
block (before line 782). This goes after we have `conversation_id` but before
the `if channel_config.instance:` chain:

```python
# Thread ownership check (before channel routing)
# Skip for roundtable-mode channels — they use hybrid routing
if channel_config.mode != "roundtable":
    owner = self._get_thread_owner(conversation_id)
    addressed_instance, addressed_prompt = self._parse_instance_prefix(
        text, self._config.instance_names
    )
    if addressed_instance != self._config.default_instance or <prefix was explicit>:
        # User explicitly addressed someone — use that instance
        instance_name = addressed_instance
        prompt = addressed_prompt
    elif owner:
        # Thread has an owner, no explicit override — route to owner
        instance_name = owner
        prompt = text
    else:
        # No owner yet — fall through to channel routing
        pass  # existing routing logic handles this
```

The tricky part: distinguishing "user typed `alpha: do this`" (explicit) from
"_parse_instance_prefix returned the channel default because nothing matched"
(implicit). Solution: modify `_parse_instance_prefix` to return a 3-tuple
`(instance_name, prompt, was_explicit)` — or create a wrapper that checks.

**Cleaner approach — add an `explicit` flag:**

Modify `_parse_instance_prefix()` return to `tuple[str, str, bool]`:

```python
def _parse_instance_prefix(
    self, text: str, known: list[str], default: str = ""
) -> tuple[str, str, bool]:
    # ... existing pattern matching ...
    # If a pattern matched: return (instance, remaining_text, True)
    # If no pattern matched: return (default, text, False)
```

This is a small change (~5 lines modified) and all existing callers just need
to unpack the third value (or ignore it with `_`). The `_handle_mention` caller
can use `_` since mentions are always explicit.

**File changes summary:**

| File | Change | Lines |
|------|--------|-------|
| `slack.py` `__init__` | Add `_thread_owners`, `_thread_owner_order`, constant | +5 |
| `slack.py` | New `_set_thread_owner()` | +15 |
| `slack.py` | New `_get_thread_owner()` | +5 |
| `slack.py` `_parse_instance_prefix` | Add `was_explicit` return value | ~5 modified |
| `slack.py` `_handle_message` | Ownership check before channel routing | +20 |
| `slack.py` `_handle_mention` | Unpack 3-tuple (trivial) | ~2 modified |
| `slack.py` `_execute_with_progress` | Call `_set_thread_owner` after posting | +1 |
| **Total** | | **~50 lines** |

**Test changes:**

| Test | What | Lines |
|------|------|-------|
| `test_slack.py` | `_parse_instance_prefix` returns 3-tuple | ~10 modified |
| `test_slack.py` | Thread owner recorded after execution | +20 |
| `test_slack.py` | Follow-up routes to owner | +25 |
| `test_slack.py` | Explicit addressing transfers ownership | +25 |
| `test_slack.py` | Bounded eviction works | +15 |
| `test_slack.py` | No owner → falls through to channel routing | +15 |
| **Total** | | **~110 lines** |

### 1.4 Dependencies

None. This is a pure addition to the routing layer.

### 1.5 Edge Cases

| Case | Handling |
|------|----------|
| Bot restarts, ownership lost | Falls back to channel routing. User prefixes once, ownership re-established. |
| Thread started by @mention in unconfigured channel | Mentioned instance gets ownership. Follow-ups route to it. |
| Two users in same thread address different instances | Last explicit addressing wins. Ownership ping-pongs. This is correct — the thread serves both users. |
| Regenerate reaction (🔄) | Uses `_message_prompts` which already stores instance_name. Ownership unchanged. |
| Thread in `[instance:alpha]` channel | Ownership is redundant — channel forces alpha. Ownership still recorded but never consulted (channel config takes priority). |
| Very old thread (owner evicted from cache) | Falls back to channel routing. Correct behavior. |

---

## Feature 2: Open Floor / Roundtable Mode

**Priority: MUST-HAVE** — The headline feature of Milestone 4.

### 2.1 Recommended Approach

When a message arrives in a `[mode:roundtable]` channel, fan out to ALL instances
concurrently. Each instance gets a roundtable-aware system prompt instructing it to
respond only if it has a unique perspective, otherwise output exactly `[PASS]`.
Filter `[PASS]` responses server-side — users never see them. Post surviving
responses with 1.5-second stagger to stay under Slack's rate limit and give
visual breathing room.

**Hybrid threading model (confirmed decision):**
- Unaddressed messages in roundtable threads → fan out to all instances
- Explicitly addressed messages → route to that one instance only
- Next unaddressed message → back to fan-out

This means roundtable threads have NO single owner. Thread ownership (Feature 1)
records them as `_ROUNDTABLE` sentinel, not as any instance name.

**How the LLM decides whether to respond:**

System prompt injection, not confidence thresholds. Reasons:
- No custom parsing of LLM output needed before the decision
- Works with any LLM provider (no provider-specific logprobs)
- The LLM's own judgment of "do I have something unique?" is good enough
- `[PASS]` is easy to detect — exact string match or startswith

The roundtable context is prepended to the prompt:

```
[ROUNDTABLE MODE — Multiple AI instances are in this conversation.
Other instances: {other_instance_names}
Respond ONLY if you have a unique, valuable perspective on this message.
If you have nothing substantive to add beyond what others would say,
respond with exactly: [PASS]
Do not repeat or rephrase what another instance has said.]
```

**Why `[PASS]` filtering is server-side:**

The LLM responds with `[PASS]` (or `[PASS] — Alpha doesn't have context here`).
The connector checks `response.strip().startswith("[PASS]")` and simply doesn't
post the message. The user never sees `[PASS]`. No special Slack formatting needed.

**Rate limiting strategy:**

Slack's hard limit: 1 message per second per channel. With N instances, worst case
is N responses in rapid succession. Solution:
- Collect all responses (concurrent execution)
- Filter `[PASS]`
- Post survivors sequentially with `asyncio.sleep(1.5)` between each
- 1.5 seconds (not 1.0) gives margin for Slack's rate limit counting and visual comfort

### 2.2 UX — What the User Sees

**Basic roundtable:**
```
#roundtable (topic: [mode:roundtable])

User:     What's the best approach for caching in this architecture?
          ⏳                                        ← hourglass on user's message
          ⚙️ Roundtable — waiting for perspectives... ← status message

  (10 seconds later — all instances have responded)

Alpha:    From a performance perspective, Redis with a write-through
          pattern gives you sub-millisecond reads...

  (1.5 second pause)

Beta:     I'd start simpler — an in-process LRU cache handles 90% of
          cases without the operational overhead of Redis...

  (status message deleted, ⏳ removed)
```

**All instances pass:**
```
User:     Thanks!
          ⏳
          ⚙️ Roundtable — waiting for perspectives...
          (status message deleted, ⏳ removed)
          (nothing posted — all instances passed)
```

No response is the correct UX for "thanks!" in a roundtable — it would be
weird for every AI to say "you're welcome!"

**Directed follow-up in roundtable thread:**
```
User:     @alpha Can you elaborate on the Redis approach?
          ⏳
          ⚙️ Working...                              ← normal single-instance status

Alpha:    Sure. The write-through pattern works by...  ← only Alpha responds
```

**Back to roundtable:**
```
User:     What are the trade-offs of each approach?     ← unaddressed
          ⏳
          ⚙️ Roundtable — waiting for perspectives...

Alpha:    The Redis approach trades operational...
Beta:     The LRU cache approach trades consistency...
```

**One instance errors:**
```
User:     Compare microservices vs monolith
          ⏳
          ⚙️ Roundtable — waiting for perspectives...

Alpha:    Here's my analysis of microservices...

  (Beta errored internally — logged, not shown to user)
  (Only Alpha's response appears)
```

### 2.3 Implementation Plan

**New method — `_execute_roundtable()`** (~90 lines):

```python
async def _execute_roundtable(
    self,
    conversation_id: str,
    prompt: str,
    channel: str,
    thread_ts: str,
    user_ts: str,
    say,
) -> None:
    """Fan out a message to all instances in roundtable mode."""
    # 1. React ⏳ on user's message
    # 2. Post status: "⚙️ Roundtable — waiting for perspectives..."
    # 3. Track as active execution (marks conversation as busy)
    # 4. Build roundtable-aware prompts for each instance
    # 5. Execute ALL instances concurrently with asyncio.gather(*tasks, return_exceptions=True)
    # 6. Filter [PASS] responses and errors
    # 7. Post surviving responses with 1.5s stagger, each with instance persona
    # 8. Record thread as roundtable (for thread ownership: _ROUNDTABLE sentinel)
    # 9. Delete status message, remove ⏳
    # 10. Process any queued messages
```

**Detailed flow of step 5 — concurrent execution:**

```python
async def _execute_single_for_roundtable(
    self, instance_name: str, instance, conversation_id: str, prompt: str,
    slack_context: dict,
) -> tuple[str, str, InstanceConfig] | None:
    """Execute one instance for roundtable. Returns (instance_name, response, instance) or None on [PASS]/error."""
    try:
        response = await self._service.execute(
            instance_name, conversation_id, prompt, slack_context=slack_context,
        )
        if response.strip().upper().startswith("[PASS]"):
            return None
        return (instance_name, response, instance)
    except Exception:
        logger.exception("Roundtable error for %s", instance_name)
        return None

# In _execute_roundtable:
tasks = [
    self._execute_single_for_roundtable(name, inst, conversation_id, rt_prompt, ctx)
    for name, inst in self._config.instances.items()
]
results = await asyncio.gather(*tasks)
responses = [r for r in results if r is not None]
```

**Modify `_handle_message()` roundtable branch** (line 786–790):

Replace the TODO block:

```python
elif channel_config.mode == "roundtable":
    # Check for explicit addressing first
    _, _, was_explicit = self._parse_instance_prefix(
        text, self._config.instance_names
    )
    if was_explicit:
        # Directed message in roundtable — single instance only
        instance_name, prompt, _ = self._parse_instance_prefix(
            text, self._config.instance_names
        )
        # Fall through to single-instance execution below
    else:
        # Unaddressed — fan out to all instances
        conversation_id = f"{channel}:{thread_ts}"
        # ... (file download, prompt enrichment same as now) ...
        await self._execute_roundtable(
            conversation_id, prompt, channel, thread_ts,
            event.get("ts", ""), say,
        )
        return
```

**Roundtable prompt builder** (~15 lines):

```python
def _build_roundtable_prompt(
    self, base_prompt: str, instance_name: str,
) -> str:
    """Wrap a prompt with roundtable context for a specific instance."""
    others = [n for n in self._config.instance_names if n != instance_name]
    return (
        f"[ROUNDTABLE MODE — Multiple AI instances are in this conversation.\n"
        f"Other instances: {', '.join(others)}\n"
        f"Respond ONLY if you have a unique, valuable perspective.\n"
        f"If you have nothing substantive to add, respond with exactly: [PASS]\n"
        f"Do not repeat or rephrase what another instance would say.]\n\n"
        f"{base_prompt}"
    )
```

**Thread ownership integration:**

In `_execute_roundtable()`, after posting responses:

```python
self._set_thread_owner(conversation_id, "_ROUNDTABLE")
```

In `_handle_message()`, the ownership check (from Feature 1) needs a roundtable
exception:

```python
owner = self._get_thread_owner(conversation_id)
if owner == "_ROUNDTABLE":
    # Roundtable thread — check for explicit addressing
    if was_explicit:
        # Route to addressed instance only (no ownership transfer)
        instance_name = addressed_instance
        prompt = addressed_prompt
    else:
        # Fan out again
        await self._execute_roundtable(...)
        return
elif owner:
    # Normal owned thread...
```

**Active execution tracking for roundtable:**

The entire roundtable fan-out is tracked as ONE active execution under the
conversation_id. Messages arriving during roundtable execution get queued
(existing behavior). After all instances complete, queued messages are processed
— if they're unaddressed, they trigger another roundtable; if addressed, they
go to one instance.

**File changes summary:**

| File | Change | Lines |
|------|--------|-------|
| `slack.py` | New `_execute_roundtable()` | +90 |
| `slack.py` | New `_execute_single_for_roundtable()` | +20 |
| `slack.py` | New `_build_roundtable_prompt()` | +15 |
| `slack.py` `_handle_message` | Replace roundtable TODO, integrate with ownership | +30 |
| `slack.py` `_handle_mention` | Handle roundtable channels on @mention | +10 |
| **Total** | | **~165 lines** |

**Test changes:**

| Test | What | Lines |
|------|------|-------|
| `test_slack.py` | Roundtable fans out to all instances | +30 |
| `test_slack.py` | `[PASS]` responses filtered | +25 |
| `test_slack.py` | All-pass produces no response | +20 |
| `test_slack.py` | Responses posted with stagger (timing) | +20 |
| `test_slack.py` | Directed message in roundtable → single instance | +25 |
| `test_slack.py` | Follow-up returns to roundtable | +25 |
| `test_slack.py` | One instance errors, others still post | +20 |
| `test_slack.py` | Message queuing during roundtable | +20 |
| **Total** | | **~185 lines** |

### 2.4 Dependencies

- **Feature 1 (Thread Ownership):** Roundtable threads need the `_ROUNDTABLE`
  sentinel in the ownership map. The `was_explicit` return from
  `_parse_instance_prefix` is also needed. Build Feature 1 first.

### 2.5 Edge Cases

| Case | Handling |
|------|----------|
| All instances `[PASS]` | No response posted. Status message deleted. ⏳ removed. Silent — correct for "thanks!" type messages. |
| One instance errors | Other responses still posted. Error logged. User sees partial roundtable (better than nothing). |
| User sends follow-up during roundtable execution | Queued via existing `_message_queues`. Processed after all instances complete. If unaddressed, triggers new roundtable. |
| Roundtable channel with only 1 instance configured | Works fine — degrades to single-instance. `[PASS]` logic is moot (only one responder). No special case needed. |
| Very long responses from multiple instances | Each posts fully. Slack handles long messages fine (up to 40,000 chars). Thread stays organized. |
| Instance is already executing in another thread | `service.execute()` acquires per-session lock. Sessions are keyed `{instance}:{conversation_id}`, so different threads don't conflict. Same thread would queue. |
| `@mention` in roundtable channel | Treated as addressed to the mentioned instance (explicit). Single-instance response. |
| `[mode:roundtable] [default:alpha]` combined | `mode` takes precedence. `default` is ignored when mode is roundtable. Explicit addressing still works. |

---

## Feature 3: Emoji Summoning

**Priority: MUST-HAVE** — High-value, low-effort. Natural interaction pattern.

### 3.1 Recommended Approach

React with a custom emoji named after an instance (`:alpha:`, `:beta:`) on any
message to summon that instance. The instance reads the reacted message (and
thread context if applicable) and responds in-thread.

**Emoji mapping strategy: Instance name = emoji name.**

No config changes needed. If the `reaction` field in the Slack event matches an
instance name, that instance is summoned. This requires creating custom emoji in
the Slack workspace named `:alpha:`, `:beta:`, etc.

**Why not use `persona.emoji`:** The persona emoji (`:robot_face:`) is shared across
instances by default. We need per-instance emoji for summoning. Custom workspace emoji
named after instances is the cleanest approach — it's self-documenting and the setup
wizard can remind users to create them.

**Fallback:** If someone reacts with an emoji that happens to match an instance name
but isn't a custom emoji (unlikely collision), it still triggers summoning. This is
a feature, not a bug — it means any emoji mapping works.

**What the instance sees:**

The summoned instance receives a prompt with the reacted message's content and
context about being summoned:

```
[<@user> summoned you by reacting with :alpha: to this message in #channel]
{message_text}
```

If the reacted message is in a thread, the instance gets the thread's
conversation context (existing session, if any).

### 3.2 UX — What the User Sees

**Basic summoning:**
```
#general:
  User:     Here's our Q4 revenue report: we grew 23% YoY...

  (User reacts with :alpha: emoji)

  Alpha:    Looking at the Q4 report, three things stand out:
            1. The 23% YoY growth outpaces the industry...
            (responds in thread under the reacted message)
```

**Multiple emoji reactions:**
```
  User:     Should we use Postgres or DynamoDB for this?

  (User reacts with :alpha: and :beta:)

  Alpha:    From a relational data modeling perspective, Postgres...
  Beta:     For this access pattern, DynamoDB would give you...
```

Each triggers independently. They execute concurrently (different sessions).

**Summoning into existing thread:**
```
  Thread started earlier with Alpha...
  Alpha:    The auth module looks solid except for...

  Other User:  What about the rate limiting?

  (Someone reacts with :beta: on "What about the rate limiting?")

  Beta:     Looking at rate limiting from a fresh perspective...
            (Beta now has this thread's conversation context)
```

**Summoning on a bot message:**
```
  Alpha:    I recommend using Redis for this cache layer.

  (User reacts with :beta: on Alpha's message)

  Beta:     I'd push back on Redis here — an in-process cache
            would be simpler for your scale...
```

### 3.3 Implementation Plan

**Extend `_handle_reaction()`** — currently handles `repeat` and `x` reactions
on bot messages only (line 906–956). We add a new branch that triggers BEFORE
the `message_ts not in self._message_prompts` early-return:

```python
async def _handle_reaction(self, event: dict, say) -> None:
    reaction = event.get("reaction", "")
    item = event.get("item", {})
    channel = item.get("channel", "")
    message_ts = item.get("ts", "")
    user = event.get("user", "")

    # --- NEW: Emoji summoning ---
    # Check if the reaction name matches an instance name
    if reaction in self._config.instance_names:
        # Don't let the bot summon itself
        if user == self._bot_user_id:
            return
        await self._handle_emoji_summon(reaction, channel, message_ts, user, say)
        return

    # --- Existing: reactions on bot messages ---
    if message_ts not in self._message_prompts:
        return
    # ... existing repeat/cancel logic ...
```

**New method — `_handle_emoji_summon()`** (~55 lines):

```python
async def _handle_emoji_summon(
    self,
    instance_name: str,
    channel: str,
    message_ts: str,
    user: str,
    say,
) -> None:
    """Handle emoji summoning — user reacted with an instance-name emoji."""
    instance = self._config.get_instance(instance_name)

    # Fetch the reacted message to get its text
    try:
        result = await self._app.client.conversations_history(
            channel=channel,
            latest=message_ts,
            inclusive=True,
            limit=1,
        )
        messages = result.get("messages", [])
        if not messages:
            return
        target_message = messages[0]
    except Exception:
        logger.exception("Could not fetch message %s for emoji summon", message_ts)
        return

    message_text = target_message.get("text", "")
    if not message_text:
        return

    # Determine thread context:
    # If the message is IN a thread, use that thread's ts
    # If the message is top-level, use the message itself as thread root
    thread_ts = target_message.get("thread_ts", message_ts)
    conversation_id = f"{channel}:{thread_ts}"

    # Get channel name for context enrichment
    channel_config = await self._get_channel_config(channel)
    channel_name = channel_config.name

    # Build summon prompt
    prompt = (
        f"[<@{user}> summoned you by reacting with :{instance_name}: "
        f"to this message in #{channel_name}]\n"
        f"{message_text}"
    )

    logger.info(
        "Emoji summon: %s summoned %s on message %s",
        user, instance_name, message_ts,
    )

    # Deduplicate: track this summon to prevent double-trigger
    summon_key = f"summon:{instance_name}:{message_ts}"
    if summon_key in self._handled_messages:
        return
    self._handled_messages.add(summon_key)

    # Execute (reuses existing flow: progress, persona, queuing)
    await self._execute_with_progress(
        instance_name, instance, conversation_id, prompt,
        channel, thread_ts, message_ts, say,
    )
```

**File changes summary:**

| File | Change | Lines |
|------|--------|-------|
| `slack.py` `_handle_reaction` | New emoji summoning branch (before existing logic) | +8 |
| `slack.py` | New `_handle_emoji_summon()` | +55 |
| **Total** | | **~63 lines** |

**Test changes:**

| Test | What | Lines |
|------|------|-------|
| `test_slack.py` | Emoji matching instance name triggers summon | +25 |
| `test_slack.py` | Non-instance emoji ignored (falls through to existing logic) | +10 |
| `test_slack.py` | Bot's own reactions ignored | +10 |
| `test_slack.py` | Reacted message text fetched and included in prompt | +20 |
| `test_slack.py` | Thread context preserved (in-thread reaction) | +15 |
| `test_slack.py` | Top-level message creates new thread | +15 |
| `test_slack.py` | Duplicate summon deduplicated | +10 |
| `test_slack.py` | Message fetch failure handled gracefully | +10 |
| **Total** | | **~115 lines** |

### 3.4 Dependencies

None. This extends `_handle_reaction` independently of other features.

Thread ownership integration is optional: after an emoji summon, the summoned
instance could get thread ownership. Decision: **don't transfer ownership on
summon.** Summoning is a one-shot consultation — the user is asking for a
perspective, not changing the conversation's direction. The existing owner (if
any) keeps the thread.

### 3.5 Edge Cases

| Case | Handling |
|------|----------|
| React with `:alpha:` AND `:beta:` | Two separate `reaction_added` events. Both trigger independently. Both execute concurrently (different session keys). Both respond in-thread. |
| React on a message in a DM | Works — DMs have a channel ID. Conversation ID is `dm:{user_id}` pattern. Instance responds in-DM thread. |
| React on a message with files/images | We only extract `text`. Files in the reacted message are NOT downloaded. The instance sees the text description but not file contents. Acceptable — summoning is about reacting to the message content. |
| Custom emoji not created in workspace | The reaction name won't match an instance name because Slack requires custom emoji to exist. If someone types `:alpha:` in a workspace without that custom emoji, Slack won't let them react with it. Non-issue. |
| Instance already executing for that thread | Existing `_active_executions` check in `_execute_with_progress` handles this — the summon message gets queued or injected. |
| React on very old message (no thread context) | Fine — the instance gets the summoned message text. No prior conversation context, which is correct for a cold summon. |
| React on own bot's message | Works — instance reads its own (or another instance's) previous response and provides commentary. |

---

## Feature 4: Streaming Response (Progressive Message Editing)

**Priority: NICE-TO-HAVE** — Polish feature. Current UX is already good with
status messages.

### 4.1 Recommended Approach

**The constraint:** `chat:write.customize` messages (which use `username` +
`icon_emoji` for persona) CANNOT be edited via `chat.update`. This is a hard
Slack API limitation with no workaround.

**Recommendation: Enhanced status message with live preview.**

The existing "⚙️ Working..." status message (posted under bot identity, editable)
becomes a streaming preview window. As tokens arrive, the status message is updated
with accumulated content. When complete, the status message is deleted and the
final response is posted with full persona (username + emoji) as today.

This is an evolution of the existing pattern, not a replacement:

1. Status message starts as: `⚙️ Working...`
2. After first tokens arrive: `⚙️ Alpha is responding:\n\n{partial_content}`
3. Updated every ~2 seconds (throttled)
4. On tool calls: switches to `⚙️ Reading files...` (existing behavior)
5. After tools complete, back to content preview
6. On completion: delete status message, post final response with persona

**Why this approach over alternatives:**

| Alternative | Problem |
|------------|---------|
| Post as bot, stream, delete+repost with persona | Notification flash on delete/repost. Message order may shift. Jarring UX. |
| Stream under bot identity permanently | Loses persona (name + emoji). Core UX regression. |
| Post persona message first, edit it | Can't edit `chat:write.customize` messages. Non-starter. |
| Skip streaming entirely | Current approach. Works fine. Streaming is polish, not essential. |

The enhanced status message gives users a live preview during long responses
while preserving the clean persona on the final message. For short responses
(< 3 seconds), users just see "⚙️ Working..." then the response — no difference
from today.

**Rate limiting on `chat.update`:**

Slack Tier 3: 50+ requests/minute. Updating every 2 seconds = 30/minute.
Comfortable margin. We throttle in the connector, not the orchestrator.

### 4.2 UX — What the User Sees

**Long response (streaming visible):**
```
User:     Explain the CAP theorem and its implications for our architecture.
          ⏳

  (0.0s)  ⚙️ Working...

  (2.0s)  ⚙️ Alpha is responding:

          The CAP theorem states that in a distributed system, you can
          only guarantee two of three properties...

  (4.0s)  ⚙️ Alpha is responding:

          The CAP theorem states that in a distributed system, you can
          only guarantee two of three properties: Consistency,
          Availability, and Partition tolerance. For your architecture,
          this means...

  (6.0s — tool call)
          ⚙️ Reading files...

  (8.0s — back to streaming)
          ⚙️ Alpha is responding:

          [accumulated content so far including post-tool additions]...

  (10.0s — complete)
          (status message deleted)
Alpha:    The CAP theorem states that in a distributed system...
          [full response with persona]
```

**Short response (streaming not visible):**
```
User:     What time is it?
          ⏳

  (0.0s)  ⚙️ Working...
  (1.5s)  (status message deleted)
Alpha:    I don't have access to the current time, but you can...
```

No difference from today for fast responses.

### 4.3 Implementation Plan

**Orchestrator changes — bubble up content deltas via `on_progress`:**

The `loop-interactive` orchestrator already yields `(token, iteration)` tuples in
`_execute_stream()`. It also already has the `_on_progress` callback. We add
progress events for content:

In `_execute_stream()`, after the `yield (token, iteration)` on line 505:

```python
# Fire content progress event for streaming
if self._on_progress:
    try:
        await self._on_progress("content:delta", {
            "token": token,
            "iteration": iteration,
        })
    except Exception:
        pass
```

**Service layer — pass through (no changes needed):**

The `on_progress` callback is already threaded from `slack.py` through
`service.execute()` to the orchestrator. The orchestrator fires the callback
directly. No service.py changes needed.

**Connector changes — streaming display in `_execute_with_progress()`:**

Add streaming state and throttle logic to the `on_progress` callback:

```python
# Streaming state
_stream_buffer = []        # accumulated tokens
_stream_last_update = 0.0  # timestamp of last chat.update
_STREAM_THROTTLE = 2.0     # seconds between updates

async def on_progress(event_type: str, data: dict) -> None:
    nonlocal _stream_buffer, _stream_last_update
    if not status_msg:
        return

    if event_type == "content:delta":
        _stream_buffer.append(data.get("token", ""))
        now = time.time()
        if now - _stream_last_update >= _STREAM_THROTTLE:
            accumulated = "".join(_stream_buffer)
            # Truncate preview to 3000 chars (Slack message limit is 40k,
            # but previews should be concise)
            preview = accumulated[:3000]
            if len(accumulated) > 3000:
                preview += "\n\n_(streaming...)_"
            text = f"⚙️ {instance.persona.name} is responding:\n\n{preview}"
            try:
                await self._app.client.chat_update(
                    channel=channel, ts=status_msg, text=text,
                )
            except Exception:
                pass
            _stream_last_update = now
        return

    # Existing event handling (tool:pre, executing, etc.)
    if event_type == "tool:pre":
        _stream_buffer.clear()  # Reset preview during tool execution
        # ... existing tool status logic ...
```

**File changes summary:**

| File | Change | Lines |
|------|--------|-------|
| `loop-interactive/__init__.py` | Add `content:delta` on_progress call after yield | +6 |
| `slack.py` `_execute_with_progress` on_progress | Streaming buffer, throttle, preview display | +35 |
| `slack.py` `_execute_with_progress` | Initialize stream state variables | +5 |
| **Total** | | **~46 lines** |

**Test changes:**

| Test | What | Lines |
|------|------|-------|
| `test_slack.py` | `content:delta` events accumulate in buffer | +20 |
| `test_slack.py` | Status message updated with preview text | +20 |
| `test_slack.py` | Throttle prevents updates more often than 2s | +15 |
| `test_slack.py` | Tool call clears stream buffer | +15 |
| `test_slack.py` | Short response: no streaming update visible | +10 |
| `test_injection.py` | `content:delta` on_progress fired during streaming | +15 |
| **Total** | | **~95 lines** |

### 4.4 Dependencies

None from other M4 features. Requires the `loop-interactive` orchestrator
(already in place).

**Note on roundtable interaction:** During roundtable, the status message shows
"⚙️ Roundtable — waiting for perspectives..." — we do NOT stream individual
instance responses during roundtable. Reason: we can't interleave streaming from
multiple instances into one status message coherently. Roundtable waits for all
to complete, then posts sequentially.

### 4.5 Edge Cases

| Case | Handling |
|------|----------|
| Response < 3 seconds | No streaming update fires (throttle hasn't elapsed). User sees "⚙️ Working..." then final response. Same as today. |
| Very long response (> 3000 chars preview) | Truncated to 3000 chars with "_(streaming...)_" indicator. Full response posted at end. |
| Multiple tool calls interrupt streaming | Each tool call clears the buffer and shows tool status. After tool completes, streaming resumes with fresh buffer. Content before tools is NOT re-shown in preview (it's in the final response). |
| `chat.update` rate limit hit | Swallowed exception (best-effort). Next update at next throttle interval. |
| User sends message during streaming (injection) | Injection queue shows "(1 message queued)" — same as today. Streaming preview continues alongside. |
| Roundtable mode | Streaming disabled for roundtable. Too confusing with multiple concurrent instances. Status message shows roundtable status only. |

---

## Feature 5: Cross-Machine Coordination

**Priority: DEFER** — Works today with zero code changes. Documentation only.

### 5.1 Recommended Approach

**This is a deployment pattern, not a code feature.**

Multiple connector processes on different machines, each with its own config file
listing different instances. Slack Socket Mode delivers events to ALL connected
handlers. Each connector filters to its own instances via existing routing logic.

**Why it already works:**

1. Machine A config: `instances: {alpha: {...}}` — only knows about alpha
2. Machine B config: `instances: {beta: {...}}` — only knows about beta
3. Both connect via Socket Mode, both receive all events
4. Message addressed to alpha → Machine A processes, Machine B hits `KeyError`
   at line 803–808 and returns silently
5. Message addressed to beta → Machine B processes, Machine A returns silently
6. Roundtable message → each machine fans out to its own instances only,
   posts its responses. Both post to the same thread. Works.

**The one requirement:** Each machine MUST have a different default instance
(or no default). If both machines have `default_instance: alpha`, but only
Machine A actually has alpha configured, Machine B will try to route unaddressed
messages to alpha, fail, and drop them silently. Not harmful, but wasteful.

Better: each machine's default_instance should be one of its own instances.
Unaddressed messages in `[default:X]` channels then get handled by whichever
machine owns instance X.

### 5.2 UX — What the User Sees

Identical to single-machine. Users don't know or care which machine is running
which instance.

### 5.3 Implementation Plan

**Code changes: None.**

All the routing, session keying, and instance validation already handles this.

**Documentation: ~80 lines** in a new `docs/MULTI_MACHINE.md`:

1. How to split instances across machines
2. Config file examples for Machine A and Machine B
3. Default instance requirements
4. Roundtable behavior across machines
5. Monitoring and troubleshooting

### 5.4 Dependencies

- Feature 2 (Roundtable) should be implemented first so the multi-machine
  roundtable behavior is tested
- Feature 1 (Thread Ownership) is per-machine (each machine tracks ownership
  independently, which is correct — a machine only needs to know about its
  own instances' threads)

### 5.5 Edge Cases

| Case | Handling |
|------|----------|
| Both machines have same instance name with different configs | Undefined behavior — both will respond. **Don't do this.** Document as a hard requirement: instance names must be unique across all machines. |
| One machine goes down | Its instances stop responding. Other machine's instances keep working. Users notice missing instances and can check systemd status. |
| Both machines have `default_instance: alpha` but only one has alpha | The other machine drops unaddressed messages silently (tries to route to alpha, KeyError, returns). Not harmful but wasteful. Document: default should be a local instance. |
| Message injection across machines | Not possible — injection goes into the local orchestrator's queue. If Machine A owns a session and Machine B receives a follow-up, Machine B can't inject into Machine A's running session. It either drops the message (if it doesn't have the instance) or starts a new session (if it does have the instance — but this means duplicate instances, which is forbidden). |
| Socket Mode reconnection race | Both machines reconnect independently. Slack handles this — Socket Mode is per-connection, not per-workspace. No coordination needed. |

---

## Implementation Order

### Recommended sequence:

```
1. Thread Ownership        ~50 lines code, ~110 lines tests     [1-2 days]
2. Emoji Summoning         ~63 lines code, ~115 lines tests     [1 day]
3. Open Floor / Roundtable ~165 lines code, ~185 lines tests    [2-3 days]
4. Streaming Response      ~46 lines code, ~95 lines tests      [1-2 days]
5. Cross-Machine Docs      ~0 lines code, ~80 lines docs        [0.5 day]
```

### Rationale:

**Thread Ownership first** because:
- Roundtable depends on the `_ROUNDTABLE` sentinel and `was_explicit` flag
- The `_parse_instance_prefix` 3-tuple change affects multiple callers
- Small, self-contained, immediately testable
- Improves UX for existing multi-instance users right away

**Emoji Summoning second** because:
- Zero dependencies on other features
- Small scope, high user-delight factor
- Extends existing `_handle_reaction` with a clean new branch
- Can be shipped independently as a quick win

**Roundtable third** because:
- Depends on Thread Ownership (needs `_ROUNDTABLE` sentinel and `was_explicit`)
- Largest feature — benefits from the routing infrastructure being settled
- Concurrent execution pattern is the most complex testing scenario

**Streaming fourth** because:
- Nice-to-have polish — current UX already works well
- Touches the orchestrator (loop-interactive) which is a shared module
- Benefits from roundtable being done (needs to know when NOT to stream)
- Can be shipped as incremental improvement

**Cross-Machine last** because:
- Zero code changes needed
- Just documentation
- Benefits from all features being implemented so the docs cover the
  full interaction matrix

### Total estimates:

| | Production code | Test code | Calendar |
|---|---|---|---|
| All 5 features | ~324 lines | ~500 lines | ~6-8 days |

---

## Shared Infrastructure

### Changes that affect multiple features:

**`_parse_instance_prefix` 3-tuple return** (Feature 1):
- Used by: Thread Ownership routing, Roundtable directed follow-ups
- All existing callers updated: `_handle_mention`, `_handle_message`
- Change is backward-compatible (just unpack with `_` for third value)

**`_thread_owners` dict** (Feature 1):
- Used by: Thread Ownership routing, Roundtable `_ROUNDTABLE` sentinel
- Single source of truth for "who owns this thread?"

**`_active_executions` dict** (existing):
- Used by: All features (roundtable tracks compound execution, streaming
  reads it, emoji summoning respects it)
- No structural changes needed — roundtable tracks as single entry

**`on_progress` callback** (existing):
- Extended by: Streaming (adds `content:delta` event type)
- Roundtable: uses separate status text ("Roundtable — waiting...")
- No interface change — just new event types

### New Slack API calls introduced:

| Feature | API Call | Tier | Rate |
|---------|----------|------|------|
| Emoji Summoning | `conversations.history` (fetch reacted message) | Tier 3 | 50+/min |
| Streaming | `chat.update` (update status with preview) | Tier 3 | 50+/min |
| Roundtable | `chat.postMessage` (multiple responses) | Special | 1/sec/channel |

All within comfortable rate limits. Roundtable's 1.5-second stagger specifically
addresses the `chat.postMessage` per-channel limit.
