"""Tests for Tier A / A1 — read-only dashboard + REST app (WU-2).

Covers the §6 adversarial hunt list:
* SQL injection via sort/order/filter — must be whitelist-rejected, never run;
* forget hard-deletes instead of archiving;
* the list leaking archived rows into (or hiding audit rows from) the wrong view;
* the dashboard app mounting into / disturbing the MCP path (must stay separate).

Query-layer and tool tests use the in-memory fixtures from conftest. DB-touching
endpoints are exercised by calling the async route functions directly (same
thread/event loop) rather than via TestClient, which would run them on a worker
thread and trip SQLite's thread affinity. TestClient is used only for the
no-DB routes (/ and /health).
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import claude_memory.server as server
from claude_memory.api.app import create_app
from claude_memory.api.routes import (
    ForgetRequest,
    MemoryListResponse,
    forget as forget_route,
    list_memories_endpoint,
)
from claude_memory.db.queries import (
    ListQueryError,
    MemoryRecord,
    get_memory,
    get_stats,
    insert_memory,
    list_memories,
    update_memory,
)
from claude_memory.mcp.tools import tool_memory_forget, tool_memory_list


def _dummy_embedding(dim: int = 384) -> list[float]:
    return [0.1] * dim


class _NoCloseConn:
    """Forwarding proxy whose close() is a no-op — keeps the in-memory DB alive
    after a tool's `finally: conn.close()` so post-call assertions can query it."""

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


# ---------------------------------------------------------------------------
# list_memories — filters, archived semantics, ordering, pagination
# ---------------------------------------------------------------------------


class TestListMemoriesFilters:
    def test_default_hides_archived(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        rows = list_memories(db_conn)
        ids = {r.id for r in rows}
        assert "mem-009" not in ids  # archived, hidden in default view
        assert len(rows) == 9

    def test_never_retrieved_matches_stats_ground_truth(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        """The chip's row set must equal the LIVE stat (archived excluded), so
        forgetting a never-retrieved row drops the count."""
        rows = list_memories(db_conn, filter="never_retrieved", limit=1000)
        assert {r.id for r in rows} == {"mem-008"}  # mem-009 archived -> excluded
        assert len(rows) == get_stats(db_conn)["never_retrieved"] == 1

    def test_unscoped_matches_stats_ground_truth(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        rows = list_memories(db_conn, filter="unscoped", limit=1000)
        ids = {r.id for r in rows}
        assert len(rows) == get_stats(db_conn)["unscoped"] == 8
        assert "mem-009" not in ids  # archived, excluded from the live view
        assert all(r.project_dir is None for r in rows)

    def test_tier_filter_reveals_archived(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        rows = list_memories(db_conn, tier="archived")
        assert {r.id for r in rows} == {"mem-009"}

    def test_type_filter(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        rows = list_memories(db_conn, type="user", limit=1000)
        assert {r.id for r in rows} == {"mem-001", "mem-010"}

    def test_sort_importance_desc(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        rows = list_memories(db_conn, sort="importance", order="desc")
        assert rows[0].id == "mem-003"  # importance 9.0, highest non-archived

    def test_pagination_is_disjoint_and_deterministic(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        page1 = [r.id for r in list_memories(db_conn, limit=3, offset=0)]
        page2 = [r.id for r in list_memories(db_conn, limit=3, offset=3)]
        assert len(page1) == 3 and len(page2) == 3
        assert set(page1).isdisjoint(page2)  # secondary id-sort makes this stable

    def test_forgotten_row_leaves_default_view(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        update_memory(db_conn, "mem-001", tier="archived")  # a "forget"
        assert "mem-001" not in {r.id for r in list_memories(db_conn)}

    def test_forgetting_never_retrieved_row_drops_the_stat(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        """E2: archiving a never-retrieved row reduces the flagged count and
        removes it from the chip — the pruning loop now shows progress."""
        assert get_stats(db_conn)["never_retrieved"] == 1  # mem-008 (live)
        update_memory(db_conn, "mem-008", tier="archived")  # forget it
        assert get_stats(db_conn)["never_retrieved"] == 0
        assert "mem-008" not in {
            r.id for r in list_memories(db_conn, filter="never_retrieved")
        }


# ---------------------------------------------------------------------------
# SQL injection defense — whitelist rejection, no execution
# ---------------------------------------------------------------------------


class TestListMemoriesInjectionDefense:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"sort": "access_count; DROP TABLE memories"},
            {"sort": "(SELECT 1)"},
            {"order": "asc; DELETE FROM memories"},
            {"filter": "1=1 OR tier IS NOT NULL"},
        ],
    )
    def test_malicious_input_raises_valueerror(
        self, db_conn: sqlite3.Connection, sample_memories, kwargs
    ) -> None:
        with pytest.raises(ValueError):
            list_memories(db_conn, **kwargs)

    def test_injection_does_not_execute(
        self, db_conn: sqlite3.Connection, sample_memories: list[MemoryRecord]
    ) -> None:
        with pytest.raises(ValueError):
            list_memories(db_conn, sort="access_count; DROP TABLE memories--")
        # Table intact, all rows still present.
        n = db_conn.execute("SELECT count(*) AS c FROM memories").fetchone()["c"]
        assert n == 10


# ---------------------------------------------------------------------------
# tool_memory_list + route wiring (400 on bad params)
# ---------------------------------------------------------------------------


class TestListEndpoint:
    async def test_tool_returns_results_and_count(
        self, db_conn: sqlite3.Connection, sample_memories, monkeypatch
    ) -> None:
        _patch_deps(monkeypatch, db_conn)
        out = await tool_memory_list(limit=5)
        assert out["count"] == len(out["results"]) <= 5
        assert "content" in out["results"][0]  # list view carries content

    async def test_route_returns_model(
        self, db_conn: sqlite3.Connection, sample_memories, monkeypatch
    ) -> None:
        _patch_deps(monkeypatch, db_conn)
        resp = await list_memories_endpoint(
            sort="access_count", order="desc", filter="never_retrieved",
            tier=None, type=None, limit=50, offset=0, include_archived=False,
        )
        assert isinstance(resp, MemoryListResponse)
        assert resp.count == 1  # live never-retrieved only (archived excluded)

    async def test_route_bad_sort_is_400(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        _patch_deps(monkeypatch, db_conn)
        with pytest.raises(HTTPException) as exc:
            await list_memories_endpoint(
                sort="nonsense", order="desc", filter=None, tier=None,
                type=None, limit=50, offset=0, include_archived=False,
            )
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# forget archives (never hard-deletes)
# ---------------------------------------------------------------------------


class TestForgetArchives:
    async def test_default_forget_archives_not_deletes(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        rec = MemoryRecord(
            id="f-1", content="x", summary=None, type="lesson", tags=[],
            created_at="t", updated_at="t", last_accessed="t", access_count=0,
            importance=5.0, tier="hot", project_dir=None, source_session=None,
            supersedes=None, consolidated_from=[], metadata={},
        )
        insert_memory(db_conn, rec, _dummy_embedding())
        _patch_deps(monkeypatch, db_conn)

        result = await tool_memory_forget("f-1")  # archive defaults to True

        assert result["action"] == "archived"
        row = get_memory(db_conn, "f-1")
        assert row is not None  # NOT hard-deleted
        assert row.tier == "archived"

    async def test_delete_route_rejects_hard_delete(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        """The served DELETE must refuse archive=false (§2 no hard-delete)."""
        _patch_deps(monkeypatch, db_conn)
        with pytest.raises(HTTPException) as exc:
            await forget_route("anything", ForgetRequest(archive=False))
        assert exc.value.status_code == 400

    async def test_delete_route_archives_by_default(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        rec = MemoryRecord(
            id="f-2", content="x", summary=None, type="lesson", tags=[],
            created_at="t", updated_at="t", last_accessed="t", access_count=0,
            importance=5.0, tier="hot", project_dir=None, source_session=None,
            supersedes=None, consolidated_from=[], metadata={},
        )
        insert_memory(db_conn, rec, _dummy_embedding())
        _patch_deps(monkeypatch, db_conn)

        result = await forget_route("f-2", None)  # no body -> archive
        assert result["action"] == "archived"
        assert get_memory(db_conn, "f-2").tier == "archived"


# ---------------------------------------------------------------------------
# A corrupt row is a server fault (not a 400) — ListQueryError is caught, a
# json decode error is not.
# ---------------------------------------------------------------------------


class TestErrorClassification:
    def test_bad_param_is_list_query_error(
        self, db_conn: sqlite3.Connection, sample_memories
    ) -> None:
        with pytest.raises(ListQueryError):
            list_memories(db_conn, sort="nope")

    async def test_corrupt_row_not_masked_as_400(
        self, db_conn: sqlite3.Connection, monkeypatch
    ) -> None:
        # Write a row with invalid JSON in tags, bypassing the normal serializer.
        db_conn.execute(
            "INSERT INTO memories (id, content, summary, type, tags, created_at, "
            "updated_at, last_accessed, access_count, importance, tier, project_dir, "
            "source_session, supersedes, consolidated_from, metadata) VALUES "
            "('bad-1','x',NULL,'lesson','{not json',' t',' t',' t',0,5.0,'hot',"
            "NULL,NULL,NULL,'[]','{}')"
        )
        _patch_deps(monkeypatch, db_conn)
        # The route only catches ListQueryError, so a JSONDecodeError propagates
        # (a 500 at the HTTP layer) rather than being mislabelled a 400.
        with pytest.raises(json.JSONDecodeError):
            await list_memories_endpoint(
                sort="created_at", order="desc", filter=None, tier=None,
                type=None, limit=50, offset=0, include_archived=False,
            )


# ---------------------------------------------------------------------------
# create_app is a SEPARATE service; the MCP path is untouched
# ---------------------------------------------------------------------------


class TestAppSeparation:
    def test_app_has_dashboard_and_api_routes(self) -> None:
        paths = {getattr(r, "path", None) for r in create_app().routes}
        assert "/" in paths
        assert "/api/v1/memories" in paths
        assert "/api/v1/stats" in paths

    async def test_mcp_tool_surface_unchanged(self) -> None:
        names = [t.name for t in await server.list_tools()]
        assert len(names) == 9  # WU-2 added no MCP tool
        assert "memory_why" in names  # WU-1's tool still there
        assert "memory_list" not in names  # list is REST-only

    def test_serves_page_and_health(self) -> None:
        client = TestClient(create_app())  # no DB access on these routes
        page = client.get("/")
        assert page.status_code == 200
        assert "johnny-five" in page.text
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
