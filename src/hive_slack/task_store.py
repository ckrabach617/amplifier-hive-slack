"""Structured read/write for TASKS.md (The Director's memory).

Replaces the fragile line-by-line parsing in dispatch.py with a proper
section-based parser. All mutations go through an asyncio.Lock and
writes use a temp-file + rename pattern for atomicity.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# Canonical section names and their render order
SECTION_ACTIVE = "Active"
SECTION_WAITING = "Waiting on Charlie"
SECTION_PARKED = "Parked"
SECTION_DONE = "Done (last 30 days)"
SECTIONS_ORDER = [SECTION_ACTIVE, SECTION_WAITING, SECTION_PARKED, SECTION_DONE]

# A field line: exactly 2-space indent, word key, colon, optional value
_FIELD_RE = re.compile(r"^  (\w[\w_]*):\s?(.*)$")


def _normalize_section(name: str) -> str:
    """Map heading variants to canonical names (e.g. '## Done' -> SECTION_DONE)."""
    if name.lower().startswith("done"):
        return SECTION_DONE
    return name


def sanitize_value(value: str) -> str:
    """Collapse a value to a single line -- no embedded newlines."""
    return re.sub(r"\s+", " ", value).strip()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """A single task entry with ordered key-value fields."""

    id: str
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class TaskFile:
    """Parsed TASKS.md structure -- sections containing task entries."""

    sections: dict[str, list[Task]] = field(default_factory=dict)

    def get_section(self, name: str) -> list[Task]:
        return self.sections.setdefault(name, [])

    def find_task(self, task_id: str) -> tuple[str, Task] | None:
        """Find a task by ID across all sections."""
        for section_name, tasks in self.sections.items():
            for task in tasks:
                if task.id == task_id:
                    return section_name, task
        return None

    def remove_task(self, task_id: str) -> Task | None:
        """Remove a task by ID from whatever section it's in."""
        for tasks in self.sections.values():
            for i, task in enumerate(tasks):
                if task.id == task_id:
                    return tasks.pop(i)
        return None


# ---------------------------------------------------------------------------
# Parse / Render
# ---------------------------------------------------------------------------


def parse_tasks(content: str) -> TaskFile:
    """Parse TASKS.md content into a TaskFile."""
    tf = TaskFile()
    for s in SECTIONS_ORDER:
        tf.sections[s] = []

    current_section: str | None = None
    current_task: Task | None = None
    last_key: str | None = None

    for line in content.split("\n"):
        stripped = line.strip()

        # Top-level heading (# Director Task Memory) -- skip
        if stripped.startswith("# ") and not stripped.startswith("## "):
            current_task = None
            last_key = None
            continue

        # Section heading (## Active, ## Done, etc.)
        if stripped.startswith("## "):
            current_section = _normalize_section(stripped[3:].strip())
            tf.sections.setdefault(current_section, [])
            current_task = None
            last_key = None
            continue

        # Blank line -- entry boundary
        if not stripped:
            current_task = None
            last_key = None
            continue

        if current_section is None:
            continue

        # New entry
        if stripped.startswith("- id: "):
            task_id = stripped[6:].strip()
            current_task = Task(id=task_id)
            tf.get_section(current_section).append(current_task)
            last_key = None
            continue

        # Field line (2-space indent, word key, colon)
        m = _FIELD_RE.match(line)
        if current_task is not None and m:
            key, value = m.group(1), m.group(2)
            current_task.fields[key] = value
            last_key = key
            continue

        # Unrecognized line inside an entry -- append to last field value
        # (handles multi-line values that leaked in from old format)
        if current_task is not None and last_key is not None:
            current_task.fields[last_key] += " " + stripped
            continue

    return tf


def render_tasks(tf: TaskFile) -> str:
    """Render a TaskFile back to TASKS.md markdown."""
    lines: list[str] = ["# Director Task Memory", ""]

    rendered: set[str] = set()
    for name in SECTIONS_ORDER:
        rendered.add(name)
        tasks = tf.sections.get(name, [])
        lines.append(f"## {name}")
        if not tasks:
            lines.append("")
            continue
        for task in tasks:
            lines.append(f"- id: {task.id}")
            for k, v in task.fields.items():
                lines.append(f"  {k}: {sanitize_value(v)}")
            lines.append("")

    # Any extra sections not in the canonical order
    for name, tasks in tf.sections.items():
        if name in rendered:
            continue
        lines.append(f"## {name}")
        if not tasks:
            lines.append("")
            continue
        for task in tasks:
            lines.append(f"- id: {task.id}")
            for k, v in task.fields.items():
                lines.append(f"  {k}: {sanitize_value(v)}")
            lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# TaskStore -- async-safe file operations
# ---------------------------------------------------------------------------


class TaskStore:
    """Async-safe, atomic read/write for TASKS.md."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # -- low-level I/O -------------------------------------------------------

    def _read(self) -> TaskFile:
        if self._path.exists():
            return parse_tasks(self._path.read_text(encoding="utf-8"))
        return parse_tasks("")

    def _write(self, tf: TaskFile) -> None:
        """Atomic write: temp-file in same dir, then os.replace."""
        content = render_tasks(tf)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), suffix=".tmp", prefix=".tasks-"
        )
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            fd = -1
            os.replace(tmp, str(self._path))
        except BaseException:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # -- public operations (all lock-protected) ------------------------------

    async def add_active(self, task_id: str, description: str) -> None:
        """Add a new task to the Active section."""
        async with self._lock:
            tf = self._read()
            task = Task(
                id=task_id,
                fields={
                    "description": sanitize_value(description[:200]),
                    "started": date.today().isoformat(),
                    "status": "worker dispatched",
                },
            )
            tf.get_section(SECTION_ACTIVE).insert(0, task)
            self._write(tf)
        logger.info("Added %s to TASKS.md Active", task_id)

    async def complete_task(self, task_id: str, summary: str) -> None:
        """Move a task from its current section to Done."""
        async with self._lock:
            tf = self._read()
            old = tf.remove_task(task_id)
            done = Task(
                id=task_id,
                fields={
                    "completed": date.today().isoformat(),
                    "summary": sanitize_value(summary),
                },
            )
            if old and old.fields.get("artifacts"):
                done.fields["artifacts"] = old.fields["artifacts"]
            tf.get_section(SECTION_DONE).insert(0, done)
            self._write(tf)
        logger.info("Moved %s to TASKS.md Done", task_id)

    async def fail_task(self, task_id: str, error: str) -> None:
        """Mark a specific task as failed (by task_id, not blind replace)."""
        async with self._lock:
            tf = self._read()
            result = tf.find_task(task_id)
            if result:
                _, task = result
                task.fields["status"] = f"failed -- {sanitize_value(error[:200])}"
            self._write(tf)
        logger.info("Marked %s as failed in TASKS.md", task_id)

    async def read_all(self) -> TaskFile:
        """Read the current state (no lock needed -- snapshot read)."""
        return self._read()
