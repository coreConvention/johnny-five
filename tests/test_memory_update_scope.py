"""memory_update can move a memory to another project scope (issue #38).

Sessions in a git worktree used to store memories under the worktree's own
path, which no later session can reach. Re-homing those memories needs a way to
change ``project_dir`` in place, keeping the id that other memories cross-link
to. These tests pin that move: the recall scope follows it, a blank scope is
refused (a move must never widen a project memory to global), and the previous
scope is kept in metadata so a bulk move can be audited and reversed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from claude_memory.api.routes import UpdateRequest, update as update_route
from claude_memory.db.queries import MemoryRecord, get_always_load, get_memory, insert_memory
from claude_memory.mcp.tools import tool_memory_update
from claude_memory.server import list_tools

WORKTREE = "Z:/Worktrees/example-repo/brave-turing-1a2b3c"
MAIN_CHECKOUT = "Z:/Personal/example-repo"


class _NoCloseConn:
    """Forwarding proxy whose close() is a no-op, so the in-memory DB outlives
    the tool's ``finally: conn.close()`` and post-call assertions can query it."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _patch_deps(monkeypatch, db_conn: sqlite3.Connection) -> None:
    monkeypatch.setattr(
        "claude_memory.mcp.tools._get_deps",
        lambda: (_NoCloseConn(db_conn), None, None),
    )


def _insert(
    db_conn: sqlite3.Connection,
    memory_id: str,
    project_dir: str | None,
    metadata: dict | None = None,
) -> None:
    now: str = datetime.now(timezone.utc).isoformat()
    record = MemoryRecord(
        id=memory_id,
        content=f"lesson {memory_id}",
        summary=None,
        type="lesson",
        tags=["kind:gotcha"],
        created_at=now,
        updated_at=now,
        last_accessed=now,
        access_count=0,
        importance=9.0,
        tier="hot",
        project_dir=project_dir,
        source_session=None,
        supersedes=None,
        consolidated_from=[],
        metadata=metadata or {},
    )
    insert_memory(db_conn, record, [0.1] * 384)


async def test_move_changes_scope_and_records_previous(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    _insert(db_conn, "wt-1", WORKTREE)
    _patch_deps(monkeypatch, db_conn)

    result: dict = await tool_memory_update("wt-1", project_dir=MAIN_CHECKOUT)

    assert result == {"updated": True, "memory_id": "wt-1"}
    moved: MemoryRecord | None = get_memory(db_conn, "wt-1")
    assert moved is not None
    assert moved.project_dir == MAIN_CHECKOUT
    assert moved.metadata["previous_project_dirs"] == [WORKTREE]


async def test_moved_memory_recalls_from_new_scope_only(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    _insert(db_conn, "wt-1", WORKTREE)
    assert "wt-1" not in get_always_load(db_conn, MAIN_CHECKOUT)
    _patch_deps(monkeypatch, db_conn)

    await tool_memory_update("wt-1", project_dir=MAIN_CHECKOUT)

    assert "wt-1" in get_always_load(db_conn, MAIN_CHECKOUT)
    # Windows spelling folds, so the backslash form of the main checkout matches.
    assert "wt-1" in get_always_load(db_conn, MAIN_CHECKOUT.replace("/", "\\"))
    assert "wt-1" not in get_always_load(db_conn, WORKTREE)


async def test_blank_project_dir_is_refused_and_changes_nothing(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    _insert(db_conn, "wt-1", WORKTREE)
    before: MemoryRecord | None = get_memory(db_conn, "wt-1")
    _patch_deps(monkeypatch, db_conn)

    for blank in ("", "   "):
        result: dict = await tool_memory_update("wt-1", project_dir=blank)
        assert result["updated"] is False
        assert "non-blank" in result["error"]

    assert get_memory(db_conn, "wt-1") == before


async def test_respelling_the_same_scope_adds_no_history(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    _insert(db_conn, "main-1", "Z:\\Personal\\example-repo")
    _patch_deps(monkeypatch, db_conn)

    result: dict = await tool_memory_update("main-1", project_dir=MAIN_CHECKOUT)

    assert result["updated"] is True
    record: MemoryRecord | None = get_memory(db_conn, "main-1")
    assert record is not None
    assert record.project_dir == MAIN_CHECKOUT
    assert "previous_project_dirs" not in record.metadata


async def test_scoping_a_global_memory_records_the_global_origin(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    _insert(db_conn, "global-1", None)
    _patch_deps(monkeypatch, db_conn)

    await tool_memory_update("global-1", project_dir=MAIN_CHECKOUT)

    record: MemoryRecord | None = get_memory(db_conn, "global-1")
    assert record is not None
    assert record.project_dir == MAIN_CHECKOUT
    assert record.metadata["previous_project_dirs"] == [None]


async def test_history_accumulates_and_other_metadata_survives(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    _insert(db_conn, "wt-1", WORKTREE, metadata={"floor": True})
    _patch_deps(monkeypatch, db_conn)

    await tool_memory_update("wt-1", project_dir="Z:/Personal/interim-repo")
    await tool_memory_update("wt-1", project_dir=MAIN_CHECKOUT)

    record: MemoryRecord | None = get_memory(db_conn, "wt-1")
    assert record is not None
    assert record.metadata == {
        "floor": True,
        "previous_project_dirs": [WORKTREE, "Z:/Personal/interim-repo"],
    }


async def test_rest_patch_moves_scope(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _insert(db_conn, "wt-1", WORKTREE)
    _patch_deps(monkeypatch, db_conn)

    result: dict = await update_route("wt-1", UpdateRequest(project_dir=MAIN_CHECKOUT))

    assert result == {"updated": True, "memory_id": "wt-1"}
    record: MemoryRecord | None = get_memory(db_conn, "wt-1")
    assert record is not None
    assert record.project_dir == MAIN_CHECKOUT


async def test_mcp_schema_advertises_project_dir() -> None:
    tools = {tool.name: tool for tool in await list_tools()}

    properties: dict = tools["memory_update"].inputSchema["properties"]

    assert properties["project_dir"]["type"] == "string"
