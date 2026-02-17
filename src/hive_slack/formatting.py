"""Formatting utilities and channel config for Slack messages.

Pure functions for converting markdown to Slack mrkdwn format,
rendering progress/status messages, and parsing channel topic
routing directives.
"""

from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ChannelConfig:
    """Parsed routing config from a channel's topic.

    Channel topics can contain [key:value] directives that control routing:
        [instance:alpha]    → all messages routed to alpha
        [mode:roundtable]   → all instances respond
        [default:alpha]     → alpha unless /name override
    """

    instance: str | None = None
    mode: str | None = None
    default: str | None = None
    threads: str | None = None  # "off" to disable threading (replies go in-channel)
    name: str = ""  # Channel name for context enrichment


def _parse_channel_topic(topic: str, known_instances: list[str]) -> ChannelConfig:
    """Parse [key:value] routing directives from a channel topic.

    Supports:
        [instance:alpha]    → all messages to alpha
        [mode:roundtable]   → all instances respond
        [default:alpha]     → alpha unless /name override

    Unknown instance names in directives are ignored.
    """
    config = ChannelConfig()

    for match in re.finditer(r"\[(\w+):(\w+)\]", topic):
        key = match.group(1).lower()
        value = match.group(2).lower()

        if key == "instance" and value in known_instances:
            config.instance = value
        elif key == "mode" and value in ("roundtable", "open"):
            config.mode = value
        elif key == "default" and value in known_instances:
            config.default = value
        elif key == "threads" and value in ("off",):
            config.threads = value

    return config


class ChannelConfigCache:
    """Caches parsed channel routing config from Slack channel topics."""

    def __init__(self, slack_client, instance_names: list[str], ttl: int = 60) -> None:
        self._client = slack_client
        self._instance_names = instance_names
        self._cache: dict[str, ChannelConfig] = {}
        self._timestamps: dict[str, float] = {}
        self._ttl = ttl

    async def get(self, channel_id: str) -> ChannelConfig:
        """Get routing config for a channel, parsed from its topic. Cached."""
        now = time.time()
        if (
            channel_id in self._cache
            and now - self._timestamps.get(channel_id, 0) < self._ttl
        ):
            return self._cache[channel_id]

        # Fetch channel info from Slack API
        channel_name = ""
        try:
            result = await self._client.conversations_info(channel=channel_id)
            channel_data = result.get("channel", {})
            topic = channel_data.get("topic", {}).get("value", "")
            channel_name = channel_data.get("name", "")
        except Exception:
            logger.warning("Could not fetch channel info for %s", channel_id)
            topic = ""

        config = _parse_channel_topic(topic, self._instance_names)
        config.name = channel_name
        self._cache[channel_id] = config
        self._timestamps[channel_id] = now

        logger.debug("Channel %s config: %s (topic: %s)", channel_id, config, topic)
        return config


def markdown_to_slack(text: str) -> str:
    """Convert standard markdown to Slack's mrkdwn format.

    Slack's mrkdwn differs from standard markdown:
    - *bold* instead of **bold**
    - <url|text> instead of [text](url)
    - No heading syntax (use bold instead)
    - No table syntax (render as monospace code block)
    - No horizontal rules (render as unicode line)

    Order of operations matters: tables and code blocks are extracted
    first so their content isn't mangled by inline formatting conversions.
    """
    protected: list[str] = []

    def _protect(content: str) -> str:
        protected.append(content)
        return f"\x00PROTECTED{len(protected) - 1}\x00"

    # 1. Protect existing code blocks
    text = re.sub(r"```[\s\S]*?```", lambda m: _protect(m.group(0)), text)

    # 2. Protect inline code
    text = re.sub(r"`[^`]+`", lambda m: _protect(m.group(0)), text)

    # 3. Extract and convert tables BEFORE inline formatting
    #    (so **bold** in cells becomes plain text in the code block)
    text = _convert_tables(text, _protect)

    # 4. Now safe to do inline formatting (tables are protected)
    # Bold: **text** → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # Links: [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # Headings: # Heading → *Heading*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # Horizontal rules: ---, ***, ___ → visual separator with spacing
    text = re.sub(
        r"^[-*_]{3,}\s*$",
        "\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n",
        text,
        flags=re.MULTILINE,
    )

    # 5. Restore all protected content
    for i, content in enumerate(protected):
        text = text.replace(f"\x00PROTECTED{i}\x00", content)

    # Clean up excessive blank lines (3+ → 2)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _convert_tables(text: str, protect_fn) -> str:
    """Find markdown tables and convert to a list format that wraps gracefully.

    Slack has no table support and code blocks break on narrow screens,
    so we render tables as structured lists that reflow naturally.
    """
    lines = text.split("\n")
    result: list[str] = []
    table_lines: list[str] = []
    in_table = False

    for line in lines:
        is_table_row = bool(re.match(r"^\s*\|.*\|\s*$", line))
        is_separator = bool(re.match(r"^\s*\|[-:\s|]+\|\s*$", line))

        if is_table_row:
            if not in_table:
                in_table = True
                table_lines = []
            if not is_separator:
                table_lines.append(line)
        else:
            if in_table:
                result.append(protect_fn(_render_table_as_list(table_lines)))
                table_lines = []
                in_table = False
            result.append(line)

    if in_table:
        result.append(protect_fn(_render_table_as_list(table_lines)))

    return "\n".join(result)


def _clean_cell(text: str) -> str:
    """Strip markdown bold from cell text."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).strip()


def _render_table_as_list(rows: list[str]) -> str:
    """Render a markdown table as a structured list that wraps gracefully.

    Two-column tables become:
        *Key:* Value
        *Key:* Value

    Multi-column tables become:
        *Row Label*
          Col2Header: value
          Col3Header: value
    """
    parsed: list[list[str]] = []
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        parsed.append(cells)

    if not parsed:
        return ""

    headers = parsed[0]
    data_rows = parsed[1:]

    if not data_rows:
        return "  ".join(f"*{_clean_cell(h)}*" for h in headers)

    # Two-column: simple key/value pairs
    if len(headers) == 2:
        lines = []
        for row in data_rows:
            key = _clean_cell(row[0]) if len(row) > 0 else ""
            val = row[1].strip() if len(row) > 1 else ""
            lines.append(f"*{key}:* {val}")
        return "\n".join(lines)

    # Multi-column: use header names as labels per data row
    lines = []
    for row in data_rows:
        row_label = _clean_cell(row[0]) if row else ""
        lines.append(f"*{row_label}*")
        for col_idx in range(1, len(headers)):
            header = _clean_cell(headers[col_idx]) if col_idx < len(headers) else ""
            value = row[col_idx].strip() if col_idx < len(row) else ""
            lines.append(f"  {header}: {value}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _friendly_tool_name(tool_name: str) -> str:
    """Convert tool module names to human-friendly descriptions."""
    friendly = {
        "read_file": "Reading files",
        "write_file": "Writing files",
        "edit_file": "Editing files",
        "bash": "Running command",
        "glob": "Searching files",
        "grep": "Searching content",
        "web_search": "Searching the web",
        "web_fetch": "Fetching web page",
        "delegate": "Delegating to agent",
        "todo": "Managing tasks",
        "LSP": "Analyzing code",
        "python_check": "Checking code quality",
        "load_skill": "Loading knowledge",
        "recipes": "Running recipe",
    }
    return friendly.get(tool_name, f"Working ({tool_name})")


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration. Empty string for <10s."""
    s = int(seconds)
    if s < 10:
        return ""
    if s < 60:
        return f"{s}s"
    m, rem = divmod(s, 60)
    return f"{m}m {rem}s" if rem else f"{m}m"


def _render_todo_status(
    todos: list[dict],
    current_tool: str,
    instance_name: str,
    duration_str: str,
    queued: int,
) -> str:
    """Render plan-mode status message with todo list."""
    lines = []

    # Header
    header = f"\u2699\ufe0f {instance_name}"
    if duration_str:
        header += f" \u00b7 {duration_str}"
    lines.append(header)
    lines.append(
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
    )

    # Categorize todos
    completed = [t for t in todos if t.get("status") == "completed"]
    in_progress = [t for t in todos if t.get("status") == "in_progress"]
    pending = [t for t in todos if t.get("status") == "pending"]

    # Completed: collapse if more than 2
    if len(completed) > 2:
        lines.append(f"\u2705  {len(completed)} completed")
    else:
        for t in completed:
            lines.append(f"\u2705  {t.get('content', '')}")

    # In-progress: always show with activeForm
    for t in in_progress:
        active = t.get("activeForm", t.get("content", ""))
        lines.append(f"\u25b8  *{active}*")

    # Pending: show first 2, collapse rest
    for t in pending[:2]:
        lines.append(f"\u25cb  {t.get('content', '')}")
    if len(pending) > 2:
        lines.append(f"    +{len(pending) - 2} more")

    # Footer: current tool + progress + queued
    total = len(todos)
    done = len(completed)
    if current_tool == "delegate":
        tool_text = "Delegating to agent"
    elif current_tool:
        tool_text = _friendly_tool_name(current_tool)
    else:
        tool_text = "Thinking"
    footer = f"\U0001f527 {tool_text} \u00b7 {done} of {total} complete"
    if queued > 0:
        footer += f" \u00b7 {queued} message{'s' if queued != 1 else ''} queued"
    lines.append(footer)

    return "\n".join(lines)
