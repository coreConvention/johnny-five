"""MCP resource definitions for claude-memory.

Resources provide read-only views of the memory database, formatted as
human-readable text for display in MCP resource endpoints.
"""

from __future__ import annotations

import json
import sqlite3

from claude_memory.config import get_settings
from claude_memory.db.connection import get_connection
from claude_memory.db.queries import MemoryRecord, _row_to_record
from claude_memory.mcp.tools import tool_memory_stats


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_conn() -> sqlite3.Connection:
    """Return a fresh database connection from current settings."""
    settings = get_settings()
    return get_connection(settings.resolve_db_path(), settings.embedding_dim)


def _format_memory(record: MemoryRecord) -> str:
    """Format a single :class:`MemoryRecord` as readable text."""
    tags: list[str] = (
        record.tags if isinstance(record.tags, list)
        else json.loads(record.tags or "[]")
    )
    tag_str: str = ", ".join(tags) if tags else "(none)"

    lines: list[str] = [
        f"[{record.id}] ({record.type}) importance={record.importance:.1f} tier={record.tier}",
        f"  tags: {tag_str}",
        f"  created: {record.created_at}  accessed: {record.last_accessed} (x{record.access_count})",
    ]

    if record.project_dir:
        lines.append(f"  project: {record.project_dir}")

    # Show a truncated preview of the content (max 200 chars).
    preview: str = record.content.replace("\n", " ").strip()
    if len(preview) > 200:
        preview = preview[:197] + "..."
    lines.append(f"  {preview}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Resource functions
# ---------------------------------------------------------------------------


async def resource_stats() -> str:
    """Return formatted database statistics.

    Displays total memory count plus breakdowns by type and tier.
    """
    stats: dict = await tool_memory_stats()

    lines: list[str] = [
        "=== Claude Memory Statistics ===",
        f"Total memories: {stats.get('total', 0)}",
        "",
        "By type:",
    ]

    by_type: dict[str, int] = stats.get("by_type", {})
    for memory_type, count in sorted(by_type.items()):
        lines.append(f"  {memory_type}: {count}")

    lines.append("")
    lines.append("By tier:")

    by_tier: dict[str, int] = stats.get("by_tier", {})
    for tier, count in sorted(by_tier.items()):
        lines.append(f"  {tier}: {count}")

    return "\n".join(lines)


async def resource_recent(limit: int = 20) -> str:
    """Return the most recently created memories.

    Parameters
    ----------
    limit:
        Maximum number of memories to return (default 20).
    """
    conn: sqlite3.Connection = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

        if not rows:
            return "No memories found."

        records: list[MemoryRecord] = [_row_to_record(row) for row in rows]
        header: str = f"=== {len(records)} Most Recent Memories ==="
        body: str = "\n\n".join(_format_memory(r) for r in records)
        return f"{header}\n\n{body}"
    finally:
        conn.close()


async def resource_types(memory_type: str) -> str:
    """Return all memories of a given type.

    Parameters
    ----------
    memory_type:
        The memory type to filter by (e.g. ``user``, ``feedback``,
        ``project``, ``reference``, ``lesson``).
    """
    conn: sqlite3.Connection = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM memories WHERE type = ? ORDER BY importance DESC, created_at DESC",
            (memory_type,),
        ).fetchall()

        if not rows:
            return f"No memories found with type '{memory_type}'."

        records: list[MemoryRecord] = [_row_to_record(row) for row in rows]
        header: str = f"=== {len(records)} Memories of type '{memory_type}' ==="
        body: str = "\n\n".join(_format_memory(r) for r in records)
        return f"{header}\n\n{body}"
    finally:
        conn.close()
