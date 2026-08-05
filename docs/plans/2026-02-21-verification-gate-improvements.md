# Verification Gate Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the two-pass verification gate so it doesn't reject differently-formatted correct output, surfaces quality signals instead of silently failing, enforces verification on retries, and escalates to Charlie after repeated failures.

**Architecture:** Three layers of change: (1) prompt improvements to the researcher and verifier workers so they produce better output and distinguish wrong-vs-differently-formatted results, (2) code changes to dispatch.py to track verified task history and enforce retry/escalation rules, (3) AGENTS.md updates so the Director knows how to present results and never downgrades verification.

**Tech Stack:** Python 3.12, pytest, asyncio, amplifier-hive-slack dispatch system

**Key reference files:**
- Design doc: `/mnt/c/Users/charl/OneDrive/Amplifier Data/TIER2-VERIFICATION-DESIGN.md`
- Implementation: `/home/charlie/amplifier-hive-slack/src/hive_slack/dispatch.py`
- Tests: `/home/charlie/amplifier-hive-slack/tests/test_dispatch.py`
- Director instructions: `/mnt/c/Users/charl/OneDrive/Amplifier Data/AGENTS.md`

**Design principle (from TIER2-VERIFICATION-DESIGN.md line 193):** "Always report (no retries) -- Disagreements are surfaced, not silently re-researched. Charlie decides what to trust."

---

## Task 1: Make researcher prompt resilient to format flexibility

The researcher prompt currently asks for structured output (Summary + numbered Claims). When a worker produces good research but in a different format, it still writes to `.outbox/` -- but sometimes workers interpret the structure requirement as rigid and don't write the file at all when they can't match it. The prompt needs to make clear: **always write the file, structured format preferred but not required.**

**Files:**
- Modify: `src/hive_slack/dispatch.py:115-125` (`_build_researcher_prompt`)
- Test: `tests/test_dispatch.py` (TestResearcherPrompt class, lines 349-366)

**Step 1: Write failing tests for new prompt requirements**

Add tests to `TestResearcherPrompt` (after line 366):

```python
def test_contains_must_save_instruction(self):
    """Researcher is told to always save the file regardless of format."""
    prompt = self.tool._build_researcher_prompt("research topic", "test-1")
    assert "must" in prompt.lower() or "always" in prompt.lower()
    assert "regardless" in prompt.lower() or "even if" in prompt.lower()

def test_structure_is_preferred_not_required(self):
    """Structured format is preferred, not mandatory."""
    prompt = self.tool._build_researcher_prompt("research topic", "test-1")
    assert "prefer" in prompt.lower() or "ideal" in prompt.lower()
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/test_dispatch.py::TestResearcherPrompt -v`
Expected: 2 FAIL (new tests), 4 PASS (existing)

**Step 3: Update `_build_researcher_prompt()` at dispatch.py:115-125**

Replace the method body with:

```python
def _build_researcher_prompt(self, task: str, task_id: str) -> str:
    """Build the researcher worker's prompt with structured output instructions."""
    return (
        "First read REMEMBER.md for available tools, existing work products, "
        "and gotchas.\n\n"
        f"{task}\n\n"
        f"You MUST always save your complete findings to `.outbox/{task_id}-research.md` "
        "regardless of how your research turns out.\n\n"
        "Ideally, structure your output with:\n"
        "- A Summary section\n"
        "- Numbered Claims with the source URL/reference for each claim\n\n"
        "This structure is preferred but not required. If your findings don't fit "
        "that format (e.g. document analysis, comparison tables, narrative research), "
        "use whatever structure best presents your findings. The critical requirement "
        "is that the file is saved with your complete work."
    )
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/test_dispatch.py::TestResearcherPrompt -v`
Expected: 6 PASS

**Step 5: Run full test suite to check for regressions**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
cd /home/charlie/amplifier-hive-slack
git add src/hive_slack/dispatch.py tests/test_dispatch.py
git commit -m "feat(dispatch): make researcher prompt format-flexible

Researcher must always save the output file. Structured Summary+Claims
format is preferred but not required -- workers can use whatever format
best fits their findings."
```

---

## Task 2: Enhance verifier prompt with quality signals

The verifier prompt is thin -- it only says to rate CONFIRMED/CONFLICTING/UNVERIFIED. It needs to: (a) distinguish factually wrong results from differently-formatted correct results, (b) produce a quality summary the Director can relay to Charlie, and (c) align with the design doc's intent that disagreements are surfaced, not hidden.

**Files:**
- Modify: `src/hive_slack/dispatch.py:101-113` (`_build_verifier_prompt`)
- Test: `tests/test_dispatch.py` (TestVerifierPrompt class, lines 324-341)

**Step 1: Write failing tests for enhanced verifier prompt**

Add tests to `TestVerifierPrompt` (after line 341):

```python
def test_contains_format_vs_accuracy_guidance(self):
    """Verifier distinguishes formatting differences from factual errors."""
    prompt = self.tool._build_verifier_prompt("test-1")
    lower = prompt.lower()
    assert "format" in lower or "presentation" in lower
    assert "factual" in lower or "accuracy" in lower

def test_contains_quality_summary_instruction(self):
    """Verifier is told to produce an overall quality summary."""
    prompt = self.tool._build_verifier_prompt("test-1")
    lower = prompt.lower()
    assert "overall" in lower or "summary" in lower
    assert "confidence" in lower

def test_contains_all_three_ratings(self):
    """Verifier prompt still contains all three rating levels."""
    prompt = self.tool._build_verifier_prompt("test-1")
    assert "CONFIRMED" in prompt
    assert "CONFLICTING" in prompt
    assert "UNVERIFIED" in prompt
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/test_dispatch.py::TestVerifierPrompt -v`
Expected: 2 FAIL (format/quality tests), existing tests PASS

**Step 3: Update `_build_verifier_prompt()` at dispatch.py:101-113**

Replace the method body with:

```python
def _build_verifier_prompt(self, task_id: str) -> str:
    """Build the verifier worker's prompt with claim-checking instructions."""
    return (
        "First read REMEMBER.md for available tools, existing work products, "
        "and gotchas.\n\n"
        f"Read `.outbox/{task_id}-research.md` -- it contains research findings "
        "with claims and sources.\n\n"
        "For each claim:\n"
        "1. Check whether the cited source actually supports the claim.\n"
        "2. Search for at least one additional source to corroborate or contradict.\n"
        "3. Rate confidence: CONFIRMED, CONFLICTING, or UNVERIFIED.\n\n"
        "Important: Judge factual accuracy, not formatting or presentation style. "
        "If the research reaches a correct conclusion but uses different numbers, "
        "framing, or structure than you'd expect, that is NOT conflicting. Rate "
        "based on whether the claims are true, not how they're presented.\n\n"
        "End your verification with an Overall Quality Summary:\n"
        "- How many claims were checked and their ratings (e.g. 5 of 7 CONFIRMED)\n"
        "- Overall confidence: HIGH (mostly confirmed), MIXED (some conflicting), "
        "or LOW (mostly unverified/conflicting)\n"
        "- Any critical issues Charlie should know about\n\n"
        f"Save your verification to `.outbox/{task_id}-verification.md`"
    )
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/test_dispatch.py::TestVerifierPrompt -v`
Expected: All PASS (existing + new)

**Step 5: Run full test suite**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/ -v`
Expected: All pass

**Step 6: Commit**

```bash
cd /home/charlie/amplifier-hive-slack
git add src/hive_slack/dispatch.py tests/test_dispatch.py
git commit -m "feat(dispatch): enhance verifier prompt with quality signals

Verifier now distinguishes factual errors from formatting differences,
and produces an Overall Quality Summary with confidence rating
(HIGH/MIXED/LOW) for the Director to relay to Charlie."
```

---

## Task 3: Track verified task history for retry enforcement

When a verified task fails and the Director retries it, the retry must also go through verification. We enforce this by tracking which task descriptions have been dispatched with `verification: true`, and auto-forcing verification on retries of the same work.

**Files:**
- Modify: `src/hive_slack/dispatch.py` (add tracking to `DispatchWorkerTool.__init__` and `execute`)
- Test: `tests/test_dispatch.py` (new `TestVerificationRetryEnforcement` class)

**Step 1: Write failing tests for retry enforcement**

Add a new test class at the end of test_dispatch.py:

```python
class TestVerificationRetryEnforcement:
    """Verified tasks stay verified on retry."""

    @pytest.fixture
    def working_dir(self, tmp_path):
        return tmp_path

    @pytest.fixture
    def manager(self):
        return FakeSessionManager()

    @pytest.fixture
    def tool(self, manager, working_dir):
        return DispatchWorkerTool(
            session_manager=manager,
            instance_name="alpha",
            working_dir=str(working_dir),
            director_conversation_id="test-channel:director",
        )

    @pytest.mark.asyncio
    async def test_retry_inherits_verification(self, tool, manager, working_dir):
        """A task retried without verification=True gets upgraded if base was verified."""
        outbox = working_dir / ".outbox"
        outbox.mkdir()

        call_count = 0

        async def mock_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First dispatch: verified research phase - simulate failure (no file)
                pass
            return "done"

        manager.execute = AsyncMock(side_effect=mock_execute)

        # First dispatch with verification=True - will fail (no research file)
        await tool.execute({
            "task": "research board members",
            "task_id": "board-research",
            "verification": True,
        })
        await asyncio.sleep(0.2)

        # Reset for retry
        call_count = 0
        manager.execute = AsyncMock(side_effect=mock_execute)

        # Retry WITHOUT verification - should be auto-upgraded
        result = await tool.execute({
            "task": "research board members",
            "task_id": "board-research-v2",
            "verification": False,
        })

        assert "verification enforced" in result.output.lower() or \
               "verified" in result.output.lower()

    @pytest.mark.asyncio
    async def test_unrelated_task_not_affected(self, tool, manager, working_dir):
        """A different task is not affected by a previous verified task."""
        outbox = working_dir / ".outbox"
        outbox.mkdir()

        manager.execute = AsyncMock(return_value="done")

        # First dispatch with verification=True
        await tool.execute({
            "task": "research board members",
            "task_id": "board-research",
            "verification": True,
        })
        await asyncio.sleep(0.2)

        # Different task without verification - should NOT be upgraded
        result = await tool.execute({
            "task": "write meeting agenda",
            "task_id": "meeting-agenda",
            "verification": False,
        })

        # Standard dispatch returns the normal STOP message
        assert "dispatched" in result.output.lower()
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/test_dispatch.py::TestVerificationRetryEnforcement -v`
Expected: FAIL (attribute errors or missing behavior)

**Step 3: Implement verification history tracking in dispatch.py**

Add to `DispatchWorkerTool.__init__` (around line 60): a set to track verified task descriptions.

```python
self._verified_tasks: set[str] = set()
```

In `execute()` (around line 266-290), after extracting `verification`, add retry detection:

```python
verification = input.get("verification", False)
task_lower = task.lower().strip()

# Track verified tasks and enforce on retry
if verification:
    self._verified_tasks.add(task_lower)
elif task_lower in self._verified_tasks:
    # Director tried to retry a verified task without verification -- upgrade it
    verification = True
    logger.info(
        "Verification enforced for task %s (previous verified attempt exists)",
        task_id,
    )
```

Update the return ToolResult (around line 293-300) to note when verification was enforced, so the Director sees it:

If verification was auto-upgraded, append to the result output: `"\n\nNote: Verification enforced -- a previous attempt of this task used verification."`

**Step 4: Run tests to verify they pass**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/test_dispatch.py::TestVerificationRetryEnforcement -v`
Expected: All PASS

**Step 5: Run full test suite**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/ -v`
Expected: All pass

**Step 6: Commit**

```bash
cd /home/charlie/amplifier-hive-slack
git add src/hive_slack/dispatch.py tests/test_dispatch.py
git commit -m "feat(dispatch): enforce verification persistence on retry

Track task descriptions that were dispatched with verification=true.
If the Director retries the same task without verification, auto-upgrade
it. Prevents silent quality rail downgrade."
```

---

## Task 4: Escalation after two verified failures

If a verified task fails twice, stop retrying and report to Charlie. The `[WORKER REPORT]` message should tell the Director to surface the failures and let Charlie decide.

**Files:**
- Modify: `src/hive_slack/dispatch.py` (add failure counter, modify report messages)
- Test: `tests/test_dispatch.py` (new `TestVerificationEscalation` class)

**Step 1: Write failing tests for escalation**

Add a new test class:

```python
class TestVerificationEscalation:
    """Verified tasks escalate to Charlie after 2 failures."""

    @pytest.fixture
    def working_dir(self, tmp_path):
        return tmp_path

    @pytest.fixture
    def manager(self):
        return FakeSessionManager()

    @pytest.fixture
    def tool(self, manager, working_dir):
        return DispatchWorkerTool(
            session_manager=manager,
            instance_name="alpha",
            working_dir=str(working_dir),
            director_conversation_id="test-channel:director",
        )

    @pytest.mark.asyncio
    async def test_second_failure_triggers_escalation(self, tool, manager, working_dir):
        """After 2 verified failures for the same task, report includes escalation."""
        outbox = working_dir / ".outbox"
        outbox.mkdir()

        # Both attempts will fail (no research file produced)
        manager.execute = AsyncMock(return_value="done")

        # First verified failure
        await tool.execute({
            "task": "research HOA bylaws",
            "task_id": "hoa-research",
            "verification": True,
        })
        await asyncio.sleep(0.2)

        # Second verified failure
        await tool.execute({
            "task": "research HOA bylaws",
            "task_id": "hoa-research-v2",
            "verification": True,
        })
        await asyncio.sleep(0.2)

        # Check the second notify contains escalation language
        notify_calls = manager.notify.call_args_list
        last_notify = notify_calls[-1][0][2]  # third positional arg is message
        assert "do not retry" in last_notify.lower() or "escalat" in last_notify.lower()
        assert "charlie" in last_notify.lower()

    @pytest.mark.asyncio
    async def test_first_failure_does_not_escalate(self, tool, manager, working_dir):
        """First verified failure reports normally without escalation."""
        outbox = working_dir / ".outbox"
        outbox.mkdir()

        manager.execute = AsyncMock(return_value="done")

        await tool.execute({
            "task": "research HOA bylaws",
            "task_id": "hoa-research",
            "verification": True,
        })
        await asyncio.sleep(0.2)

        notify_calls = manager.notify.call_args_list
        last_notify = notify_calls[-1][0][2]
        assert "do not retry" not in last_notify.lower()
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/test_dispatch.py::TestVerificationEscalation -v`
Expected: FAIL

**Step 3: Implement failure tracking and escalation**

Add to `DispatchWorkerTool.__init__`:

```python
self._verified_failure_counts: dict[str, int] = {}
```

In `_run_verified_worker()`, when a failure is reported (the notify calls at lines 149-156, 157-165, 168-176), increment the counter:

```python
task_lower = task.lower().strip()
self._verified_failure_counts[task_lower] = (
    self._verified_failure_counts.get(task_lower, 0) + 1
)
```

When the failure count reaches 2, append escalation language to the `[WORKER REPORT]`:

```python
if self._verified_failure_counts.get(task_lower, 0) >= 2:
    report += (
        "\n\nThis verified task has failed twice. "
        "Do NOT retry. Report the failures to Charlie and let her decide next steps."
    )
```

On success, clear the counter for that task.

**Step 4: Run tests to verify they pass**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/test_dispatch.py::TestVerificationEscalation -v`
Expected: All PASS

**Step 5: Run full test suite**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/ -v`
Expected: All pass

**Step 6: Commit**

```bash
cd /home/charlie/amplifier-hive-slack
git add src/hive_slack/dispatch.py tests/test_dispatch.py
git commit -m "feat(dispatch): escalate to Charlie after 2 verified failures

Track verified failure counts per task description. After 2 failures,
the worker report tells the Director to stop retrying and surface the
issue to Charlie for a decision."
```

---

## Task 5: Update Director instructions in AGENTS.md

The Director's AGENTS.md needs three updates: (1) a concrete template for presenting verified results, (2) explicit instructions never to downgrade verification on retry, and (3) guidance on escalation.

**Files:**
- Modify: `/mnt/c/Users/charl/OneDrive/Amplifier Data/AGENTS.md:134-175` (Tier 2 and Dispatching sections)

**Step 1: Read current AGENTS.md Tier 2 section**

Read the file and locate lines 134-175 (Tier 2 classification and dispatching instructions).

**Step 2: Update the Tier 2 section (around lines 137-141)**

Replace the brief verification instructions with a complete block:

```markdown
Set `verification: true` when dispatching. The dispatch tool automatically
chains a researcher and verifier. Present the results to Charlie as a
unified answer with inline confidence markers. Example format:

> Based on verified research (Overall: HIGH confidence, 5 of 6 claims confirmed):
>
> [Main findings in natural language]
>
> Note: One claim was CONFLICTING -- [brief explanation of the disagreement
> and both sources]. Charlie, you may want to look at this one.

Rules for verified tasks:
- NEVER retry a verified task without `verification: true`. The dispatch
  tool enforces this, but do not attempt to work around it.
- If a verified task fails twice, the worker report will tell you to
  stop retrying. Surface the failures to Charlie with what was attempted
  and what went wrong. Let her decide next steps.
- CONFLICTING and UNVERIFIED ratings are information, not failures.
  Always surface them -- Charlie decides what to trust.
```

**Step 3: Verify the file reads correctly**

Read back the modified section to confirm formatting and content.

**Step 4: Commit**

```bash
cd /home/charlie/amplifier-hive-slack
# AGENTS.md is in the OneDrive working dir, not the repo.
# This is a documentation-only change -- no code commit needed.
# The file is already tracked by the Director's working directory.
```

Note: AGENTS.md lives in the Director's working directory, not the git repo. No git commit for this step.

---

## Task 6: Run full verification and push

**Step 1: Run the complete test suite**

Run: `cd /home/charlie/amplifier-hive-slack && python -m pytest tests/ -v`
Expected: All tests pass (existing + new from Tasks 1-4)

**Step 2: Run python quality checks**

Run: `python_check(paths=["/home/charlie/amplifier-hive-slack/src/hive_slack/dispatch.py"])`
Expected: Clean (no lint errors, no type errors)

**Step 3: Review git log**

Run: `cd /home/charlie/amplifier-hive-slack && git log --oneline -5`
Expected: 4 new commits (Tasks 1-4)

**Step 4: Push to fork**

```bash
cd /home/charlie/amplifier-hive-slack
git push fork main
```

**IMPORTANT:** Push to `fork` remote ONLY. Never push to `origin`.

**Step 5: Restart the service**

```bash
cd /home/charlie/amplifier-hive-slack && source .venv/bin/activate
python -m hive_slack.main service restart
```

---

## Summary of Changes

| Task | What Changes | Why |
|------|-------------|-----|
| 1 | Researcher prompt: file save is mandatory, format is flexible | Stop workers from not writing output when format doesn't fit |
| 2 | Verifier prompt: accuracy vs format distinction, quality summary | Give Charlie a confidence signal, not just pass/fail |
| 3 | Dispatch: track verified tasks, auto-upgrade retries | Director can't silently drop verification rails |
| 4 | Dispatch: count verified failures, escalate after 2 | Prevent infinite retry loops, surface to Charlie |
| 5 | AGENTS.md: presentation template, retry rules, escalation | Director knows exactly how to present results and when to stop |
| 6 | Full test run, quality check, push, restart | Ship it |