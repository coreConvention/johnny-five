"""memory_update can be made conditional on the content the caller read (issue #52).

An update replaces a memory's whole content. Without a condition, two callers
that read the same memory, each change one part and write it back erase each
other: the later write wins and neither gets an error. These tests pin the
optional ``expected_content_sha256`` condition: the second of two such writers
is refused, a write that changed the content between the check and the
``UPDATE`` is caught too, a matching condition succeeds and says it was
checked, and a caller that sends no condition sees exactly today's behaviour.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from claude_memory.api.routes import UpdateRequest, update as update_route
from claude_memory.db.queries import MemoryRecord, get_memory, insert_memory, update_memory
from claude_memory.mcp.tools import tool_memory_get, tool_memory_update
from claude_memory.server import list_tools
from tests.conftest import MockEncoder

ORIGINAL = "roadmap\n- slot A1\n"


class _NoCloseConn:
    """Forwarding proxy whose close() is a no-op, so the in-memory DB outlives
    the tool's ``finally: conn.close()`` and post-call assertions can query it."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _patch_deps(monkeypatch, db_conn: sqlite3.Connection, encoder=None) -> None:
    monkeypatch.setattr(
        "claude_memory.mcp.tools._get_deps",
        lambda: (_NoCloseConn(db_conn), encoder or MockEncoder(), None),
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _insert(db_conn: sqlite3.Connection, memory_id: str = "note-1") -> None:
    now: str = datetime.now(timezone.utc).isoformat()
    record = MemoryRecord(
        id=memory_id,
        content=ORIGINAL,
        summary=None,
        type="project",
        tags=["kind:roadmap"],
        created_at=now,
        updated_at=now,
        last_accessed=now,
        access_count=0,
        importance=9.0,
        tier="hot",
        project_dir="Z:/Personal/example-repo",
        source_session=None,
        supersedes=None,
        consolidated_from=[],
        metadata={},
    )
    insert_memory(db_conn, record, [0.1] * 384)
    db_conn.commit()


def _content(db_conn: sqlite3.Connection, memory_id: str = "note-1") -> str:
    record: MemoryRecord | None = get_memory(db_conn, memory_id)
    assert record is not None
    return record.content


async def test_second_of_two_writers_who_read_the_same_content_is_refused(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    _insert(db_conn)
    _patch_deps(monkeypatch, db_conn)
    read_hash: str = _sha(ORIGINAL)  # both writers read the same text

    first: dict = await tool_memory_update(
        "note-1", content=ORIGINAL + "- slot A2\n", expected_content_sha256=read_hash
    )
    second: dict = await tool_memory_update(
        "note-1", content=ORIGINAL + "- slot A3\n", expected_content_sha256=read_hash
    )

    assert first == {"updated": True, "memory_id": "note-1", "condition_checked": True}
    assert second["updated"] is False
    assert second["conflict"] is True
    assert "changed since you read it" in second["error"]
    # The first writer's entry survives; the second wrote nothing.
    assert _content(db_conn) == ORIGINAL + "- slot A2\n"


async def test_a_write_landing_between_the_check_and_the_update_is_caught(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    """The hash is checked before the slow re-embed; the UPDATE must re-check.

    The encoder stands in for the other writer: while this update is encoding,
    it commits a change to the same memory. A pre-check alone would let the
    update overwrite it.
    """
    _insert(db_conn)

    class _InterloperEncoder(MockEncoder):
        def encode(self, text: str) -> list[float]:
            update_memory(db_conn, "note-1", content="written by someone else")
            db_conn.commit()
            return super().encode(text)

    _patch_deps(monkeypatch, db_conn, _InterloperEncoder())

    result: dict = await tool_memory_update(
        "note-1",
        content=ORIGINAL + "- slot A2\n",
        tags=["kind:roadmap", "edited"],
        expected_content_sha256=_sha(ORIGINAL),
    )

    assert result["updated"] is False
    assert result["conflict"] is True
    record: MemoryRecord | None = get_memory(db_conn, "note-1")
    assert record is not None
    assert record.content == "written by someone else"
    assert record.tags == ["kind:roadmap"]  # no part of the refused write landed


async def test_matching_condition_succeeds_in_either_hex_case(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    _insert(db_conn)
    _patch_deps(monkeypatch, db_conn)

    result: dict = await tool_memory_update(
        "note-1", content="v2", expected_content_sha256=_sha(ORIGINAL).upper()
    )

    assert result == {"updated": True, "memory_id": "note-1", "condition_checked": True}
    assert _content(db_conn) == "v2"


async def test_condition_also_guards_an_update_that_leaves_content_alone(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    _insert(db_conn)
    _patch_deps(monkeypatch, db_conn)

    stale: dict = await tool_memory_update(
        "note-1", importance=3.0, expected_content_sha256=_sha("something else")
    )
    fresh: dict = await tool_memory_update(
        "note-1", importance=3.0, expected_content_sha256=_sha(ORIGINAL)
    )

    assert stale["conflict"] is True
    assert fresh["condition_checked"] is True
    record: MemoryRecord | None = get_memory(db_conn, "note-1")
    assert record is not None
    assert record.importance == 3.0


async def test_no_condition_behaves_as_before(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _insert(db_conn)
    _patch_deps(monkeypatch, db_conn)

    first: dict = await tool_memory_update("note-1", content=ORIGINAL + "- slot A2\n")
    second: dict = await tool_memory_update("note-1", content=ORIGINAL + "- slot A3\n")

    # Same answer as before the option existed (no condition_checked key), and
    # the later write still replaces the earlier one.
    assert first == {"updated": True, "memory_id": "note-1"}
    assert second == {"updated": True, "memory_id": "note-1"}
    assert _content(db_conn) == ORIGINAL + "- slot A3\n"


@pytest.mark.parametrize("bad", ["", "abc", "g" * 64, _sha(ORIGINAL) + "0"])
async def test_malformed_condition_is_refused_and_writes_nothing(
    db_conn: sqlite3.Connection, monkeypatch, bad: str
) -> None:
    _insert(db_conn)
    _patch_deps(monkeypatch, db_conn)

    result: dict = await tool_memory_update("note-1", content="v2", expected_content_sha256=bad)

    assert result["updated"] is False
    assert "64 hexadecimal characters" in result["error"]
    assert _content(db_conn) == ORIGINAL


async def test_get_returns_the_hash_to_send_back(db_conn: sqlite3.Connection, monkeypatch) -> None:
    _insert(db_conn)
    _patch_deps(monkeypatch, db_conn)

    result: dict = await tool_memory_get("note-1")

    assert result["content_sha256"] == _sha(ORIGINAL)


async def test_rest_patch_answers_409_on_a_stale_condition(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    _insert(db_conn)
    _patch_deps(monkeypatch, db_conn)

    ok: dict = await update_route(
        "note-1", UpdateRequest(content="v2", expected_content_sha256=_sha(ORIGINAL))
    )
    with pytest.raises(HTTPException) as refused:
        await update_route(
            "note-1", UpdateRequest(content="v3", expected_content_sha256=_sha(ORIGINAL))
        )

    assert ok == {"updated": True, "memory_id": "note-1", "condition_checked": True}
    assert refused.value.status_code == 409
    assert "changed since you read it" in refused.value.detail
    assert _content(db_conn) == "v2"


async def test_rest_condition_alone_is_not_an_update(
    db_conn: sqlite3.Connection, monkeypatch
) -> None:
    _insert(db_conn)
    _patch_deps(monkeypatch, db_conn)

    with pytest.raises(HTTPException) as refused:
        await update_route("note-1", UpdateRequest(expected_content_sha256=_sha(ORIGINAL)))

    assert refused.value.status_code == 400


def test_rest_rejects_a_malformed_condition() -> None:
    with pytest.raises(ValidationError):
        UpdateRequest(content="v2", expected_content_sha256="not-a-hash")


async def test_mcp_schema_advertises_the_condition() -> None:
    tools = {tool.name: tool for tool in await list_tools()}

    properties: dict = tools["memory_update"].inputSchema["properties"]

    assert properties["expected_content_sha256"]["type"] == "string"
