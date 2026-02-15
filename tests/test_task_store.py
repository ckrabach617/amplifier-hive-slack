"""Tests for task_store.py -- TASKS.md parsing, rendering, and TaskStore operations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hive_slack.task_store import (
    SECTION_ACTIVE,
    SECTION_DONE,
    SECTION_PARKED,
    SECTION_WAITING,
    Task,
    TaskStore,
    parse_tasks,
    render_tasks,
    sanitize_value,
)

# ---------------------------------------------------------------------------
# sanitize_value
# ---------------------------------------------------------------------------


class TestSanitizeValue:
    def test_single_line_unchanged(self):
        assert sanitize_value("hello world") == "hello world"

    def test_collapses_newlines(self):
        assert (
            sanitize_value("line one\nline two\nline three")
            == "line one line two line three"
        )

    def test_collapses_multiple_spaces(self):
        assert sanitize_value("too   many    spaces") == "too many spaces"

    def test_strips_whitespace(self):
        assert sanitize_value("  padded  \n  ") == "padded"

    def test_empty_string(self):
        assert sanitize_value("") == ""


# ---------------------------------------------------------------------------
# parse_tasks -- empty / minimal
# ---------------------------------------------------------------------------


EMPTY_FILE = ""

MINIMAL_FILE = """# Director Task Memory

## Active

## Waiting on Charlie

## Parked

## Done (last 30 days)
"""


class TestParseEmpty:
    def test_empty_string_creates_all_sections(self):
        tf = parse_tasks(EMPTY_FILE)
        for section in [SECTION_ACTIVE, SECTION_WAITING, SECTION_PARKED, SECTION_DONE]:
            assert section in tf.sections

    def test_minimal_file_has_empty_sections(self):
        tf = parse_tasks(MINIMAL_FILE)
        assert tf.get_section(SECTION_ACTIVE) == []
        assert tf.get_section(SECTION_DONE) == []


# ---------------------------------------------------------------------------
# parse_tasks -- well-formed content
# ---------------------------------------------------------------------------

WELL_FORMED = """# Director Task Memory

## Active
- id: fire-pit-research
  description: Fire pit placement options for backyard
  started: 2026-02-14
  status: worker dispatched

## Waiting on Charlie
- id: font-qa
  description: SHS Heritage font v4 visual QA
  started: 2026-02-14
  waiting_for: Charlie to review proof sheet
  summary: Build complete, 78 glyphs
  artifacts: SHS_Font/SHSHeritage-TestSheet.png

## Parked
- id: garden-layout
  description: Garden layout design for backyard
  parked: 2026-02-08
  reason: Waiting for spring measurements
  context: Had initial ideas for three zones
  resume_hint: Ask Charlie if she has measurements yet

## Done (last 30 days)
- id: deck-stain
  completed: 2026-02-14
  summary: Top 3 semi-transparent stains compared. TWP 1500 recommended.

- id: email-rewrite
  completed: 2026-02-14
  summary: Handled inline (Tier 1). Professional tone applied.
"""


class TestParseWellFormed:
    def test_active_section(self):
        tf = parse_tasks(WELL_FORMED)
        active = tf.get_section(SECTION_ACTIVE)
        assert len(active) == 1
        assert active[0].id == "fire-pit-research"
        assert active[0].fields["status"] == "worker dispatched"

    def test_waiting_section(self):
        tf = parse_tasks(WELL_FORMED)
        waiting = tf.get_section(SECTION_WAITING)
        assert len(waiting) == 1
        assert waiting[0].id == "font-qa"
        assert waiting[0].fields["waiting_for"] == "Charlie to review proof sheet"

    def test_parked_section(self):
        tf = parse_tasks(WELL_FORMED)
        parked = tf.get_section(SECTION_PARKED)
        assert len(parked) == 1
        assert parked[0].id == "garden-layout"
        assert (
            parked[0].fields["resume_hint"] == "Ask Charlie if she has measurements yet"
        )

    def test_done_section(self):
        tf = parse_tasks(WELL_FORMED)
        done = tf.get_section(SECTION_DONE)
        assert len(done) == 2
        assert done[0].id == "deck-stain"
        assert done[1].id == "email-rewrite"

    def test_find_task(self):
        tf = parse_tasks(WELL_FORMED)
        result = tf.find_task("garden-layout")
        assert result is not None
        section, task = result
        assert section == SECTION_PARKED
        assert task.fields["reason"] == "Waiting for spring measurements"

    def test_find_missing_task(self):
        tf = parse_tasks(WELL_FORMED)
        assert tf.find_task("nonexistent") is None

    def test_remove_task(self):
        tf = parse_tasks(WELL_FORMED)
        removed = tf.remove_task("deck-stain")
        assert removed is not None
        assert removed.id == "deck-stain"
        assert len(tf.get_section(SECTION_DONE)) == 1


# ---------------------------------------------------------------------------
# parse_tasks -- corrupted / multi-line content
# ---------------------------------------------------------------------------

CORRUPTED_SUMMARY = """# Director Task Memory

## Active

## Waiting on Charlie

## Parked

## Done (last 30 days)
- id: fir-floor-sealer-research
  completed: 2026-02-14
  summary: Research complete. Here's what I found for Douglas fir floor sealers:

## Top Recommendation: Minwax Ultimate Floor Finish

**Why it's best for your situation:**
- Dog-durable with 4 coats

- id: deck-stain
  completed: 2026-02-14
  summary: TWP 1500 recommended.
"""


class TestParseCorrupted:
    def test_corrupted_entry_still_parses(self):
        tf = parse_tasks(CORRUPTED_SUMMARY)
        done = tf.get_section(SECTION_DONE)
        # The fir-floor entry parses with its first-line summary
        fir = next((t for t in done if t.id == "fir-floor-sealer-research"), None)
        assert fir is not None
        assert "Research complete" in fir.fields["summary"]

    def test_subsequent_entry_survives_corruption(self):
        tf = parse_tasks(CORRUPTED_SUMMARY)
        # deck-stain lands in the spurious section (after the leaked heading),
        # but it still parses correctly -- data is not lost
        result = tf.find_task("deck-stain")
        assert result is not None
        _, deck = result
        assert "TWP 1500" in deck.fields["summary"]

    def test_corrupted_heading_becomes_extra_section(self):
        tf = parse_tasks(CORRUPTED_SUMMARY)
        # The leaked "## Top Recommendation" becomes a spurious section
        assert "Top Recommendation: Minwax Ultimate Floor Finish" in tf.sections


# ---------------------------------------------------------------------------
# parse_tasks -- "## Done" variant (no parenthetical)
# ---------------------------------------------------------------------------

DONE_VARIANT = """# Director Task Memory

## Active

## Done
- id: old-task
  completed: 2026-01-01
  summary: Something old.
"""


class TestDoneVariant:
    def test_done_heading_normalized(self):
        tf = parse_tasks(DONE_VARIANT)
        done = tf.get_section(SECTION_DONE)
        assert len(done) == 1
        assert done[0].id == "old-task"


# ---------------------------------------------------------------------------
# render_tasks
# ---------------------------------------------------------------------------


class TestRender:
    def test_empty_file_renders_all_sections(self):
        tf = parse_tasks("")
        output = render_tasks(tf)
        assert "## Active" in output
        assert "## Waiting on Charlie" in output
        assert "## Parked" in output
        assert "## Done (last 30 days)" in output

    def test_round_trip_preserves_data(self):
        tf = parse_tasks(WELL_FORMED)
        output = render_tasks(tf)
        tf2 = parse_tasks(output)

        for section in [SECTION_ACTIVE, SECTION_WAITING, SECTION_PARKED, SECTION_DONE]:
            orig = tf.get_section(section)
            reparsed = tf2.get_section(section)
            assert len(orig) == len(reparsed), f"Section {section} length mismatch"
            for a, b in zip(orig, reparsed):
                assert a.id == b.id
                assert a.fields == b.fields

    def test_render_sanitizes_multiline_values(self):
        tf = parse_tasks("")
        task = Task(id="test", fields={"summary": "line one\nline two\nline three"})
        tf.get_section(SECTION_DONE).append(task)
        output = render_tasks(tf)
        assert "line one line two line three" in output
        assert "\nline two" not in output

    def test_render_includes_extra_sections(self):
        tf = parse_tasks("")
        tf.sections["Custom Section"] = [Task(id="custom-1", fields={"note": "hello"})]
        output = render_tasks(tf)
        assert "## Custom Section" in output
        assert "- id: custom-1" in output


# ---------------------------------------------------------------------------
# TaskStore -- async operations
# ---------------------------------------------------------------------------


@pytest.fixture
def task_store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "TASKS.md")


class TestTaskStoreAddActive:
    def test_creates_file_if_missing(self, task_store: TaskStore):
        asyncio.get_event_loop().run_until_complete(
            task_store.add_active("test-task", "Do something")
        )
        assert task_store.path.exists()

    def test_adds_to_active_section(self, task_store: TaskStore):
        asyncio.get_event_loop().run_until_complete(
            task_store.add_active("test-task", "Do something")
        )
        tf = parse_tasks(task_store.path.read_text())
        active = tf.get_section(SECTION_ACTIVE)
        assert len(active) == 1
        assert active[0].id == "test-task"
        assert active[0].fields["status"] == "worker dispatched"
        assert active[0].fields["description"] == "Do something"

    def test_truncates_long_descriptions(self, task_store: TaskStore):
        long_desc = "x" * 500
        asyncio.get_event_loop().run_until_complete(
            task_store.add_active("long-task", long_desc)
        )
        tf = parse_tasks(task_store.path.read_text())
        assert len(tf.get_section(SECTION_ACTIVE)[0].fields["description"]) <= 200

    def test_multiple_tasks_ordered_newest_first(self, task_store: TaskStore):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(task_store.add_active("first", "First task"))
        loop.run_until_complete(task_store.add_active("second", "Second task"))
        tf = parse_tasks(task_store.path.read_text())
        active = tf.get_section(SECTION_ACTIVE)
        assert len(active) == 2
        assert active[0].id == "second"  # newest first (insert at 0)
        assert active[1].id == "first"


class TestTaskStoreCompleteTask:
    def test_moves_from_active_to_done(self, task_store: TaskStore):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(task_store.add_active("my-task", "Do it"))
        loop.run_until_complete(task_store.complete_task("my-task", "All done"))

        tf = parse_tasks(task_store.path.read_text())
        assert len(tf.get_section(SECTION_ACTIVE)) == 0
        done = tf.get_section(SECTION_DONE)
        assert len(done) == 1
        assert done[0].id == "my-task"
        assert done[0].fields["summary"] == "All done"

    def test_sanitizes_multiline_summary(self, task_store: TaskStore):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(task_store.add_active("ml-task", "Multi-line test"))
        loop.run_until_complete(
            task_store.complete_task(
                "ml-task", "Line one\n## Heading\n- bullet\n- bullet2"
            )
        )
        tf = parse_tasks(task_store.path.read_text())
        done = tf.get_section(SECTION_DONE)
        summary = done[0].fields["summary"]
        assert "\n" not in summary
        assert "Line one" in summary
        assert "bullet" in summary

    def test_preserves_other_active_tasks(self, task_store: TaskStore):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(task_store.add_active("keep-me", "Stay active"))
        loop.run_until_complete(task_store.add_active("complete-me", "Going done"))
        loop.run_until_complete(task_store.complete_task("complete-me", "Finished"))

        tf = parse_tasks(task_store.path.read_text())
        active = tf.get_section(SECTION_ACTIVE)
        assert len(active) == 1
        assert active[0].id == "keep-me"

    def test_completing_nonexistent_task_adds_to_done(self, task_store: TaskStore):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(task_store.complete_task("ghost", "Completed anyway"))
        tf = parse_tasks(task_store.path.read_text())
        done = tf.get_section(SECTION_DONE)
        assert len(done) == 1
        assert done[0].id == "ghost"


class TestTaskStoreFailTask:
    def test_marks_correct_task_failed(self, task_store: TaskStore):
        """The original bug: _fail_task used blind .replace() and could
        mark the wrong task. This test ensures the correct task is targeted."""
        loop = asyncio.get_event_loop()
        loop.run_until_complete(task_store.add_active("task-a", "First task"))
        loop.run_until_complete(task_store.add_active("task-b", "Second task"))

        # Fail task-b specifically
        loop.run_until_complete(task_store.fail_task("task-b", "Something broke"))

        tf = parse_tasks(task_store.path.read_text())
        active = tf.get_section(SECTION_ACTIVE)

        task_a = next(t for t in active if t.id == "task-a")
        task_b = next(t for t in active if t.id == "task-b")

        assert task_a.fields["status"] == "worker dispatched"  # untouched
        assert "failed" in task_b.fields["status"]
        assert "Something broke" in task_b.fields["status"]

    def test_fail_nonexistent_task_no_crash(self, task_store: TaskStore):
        loop = asyncio.get_event_loop()
        # Should not raise
        loop.run_until_complete(task_store.fail_task("ghost", "error"))


class TestTaskStoreConcurrency:
    def test_concurrent_completions_no_data_loss(self, task_store: TaskStore):
        """Two workers completing at the same time must not clobber each other."""
        loop = asyncio.get_event_loop()
        loop.run_until_complete(task_store.add_active("task-a", "First"))
        loop.run_until_complete(task_store.add_active("task-b", "Second"))
        loop.run_until_complete(task_store.add_active("task-c", "Third"))

        async def complete_all():
            # Launch concurrent completions
            await asyncio.gather(
                task_store.complete_task("task-a", "Done A"),
                task_store.complete_task("task-b", "Done B"),
                task_store.complete_task("task-c", "Done C"),
            )

        loop.run_until_complete(complete_all())

        tf = parse_tasks(task_store.path.read_text())
        assert len(tf.get_section(SECTION_ACTIVE)) == 0
        done = tf.get_section(SECTION_DONE)
        done_ids = {t.id for t in done}
        assert done_ids == {"task-a", "task-b", "task-c"}


class TestTaskStoreAtomicWrite:
    def test_no_temp_files_left_on_success(self, task_store: TaskStore):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(task_store.add_active("test", "Test"))
        tmp_files = list(task_store.path.parent.glob(".tasks-*"))
        assert len(tmp_files) == 0

    def test_existing_content_preserved_on_add(self, task_store: TaskStore):
        """Verify that adding a task doesn't wipe existing Done entries."""
        # Pre-populate with a done task
        task_store.path.parent.mkdir(parents=True, exist_ok=True)
        task_store.path.write_text(
            "# Director Task Memory\n\n"
            "## Active\n\n"
            "## Waiting on Charlie\n\n"
            "## Parked\n\n"
            "## Done (last 30 days)\n"
            "- id: old-task\n"
            "  completed: 2026-01-01\n"
            "  summary: Old work\n"
        )
        loop = asyncio.get_event_loop()
        loop.run_until_complete(task_store.add_active("new-task", "New work"))

        tf = parse_tasks(task_store.path.read_text())
        assert len(tf.get_section(SECTION_ACTIVE)) == 1
        assert len(tf.get_section(SECTION_DONE)) == 1
        assert tf.get_section(SECTION_DONE)[0].id == "old-task"


class TestTaskStoreReadAll:
    def test_read_all_returns_snapshot(self, task_store: TaskStore):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(task_store.add_active("snap", "Snapshot test"))
        tf = loop.run_until_complete(task_store.read_all())
        assert len(tf.get_section(SECTION_ACTIVE)) == 1
        assert tf.get_section(SECTION_ACTIVE)[0].id == "snap"
