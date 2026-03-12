"""Tests for the database query layer.

Uses the in-memory SQLite fixture from conftest (no sqlite-vec required).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from claude_memory.db.queries import (
    MemoryRecord,
    bulk_update_importance,
    delete_memory,
    get_always_load,
    get_memories_by_tier,
    get_memory,
    get_stats,
    insert_memory,
    update_access,
    update_memory,
    update_tiers,
)

from tests.conftest import MockEncoder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    id: str = "test-001",
    content: str = "Some test content",
    type: str = "user",
    tags: list[str] | None = None,
    importance: float = 5.0,
    tier: str = "hot",
    access_count: int = 0,
    project_dir: str | None = None,
    last_accessed: str | None = None,
) -> MemoryRecord:
    """Build a :class:`MemoryRecord` with sensible defaults."""
    now: str = datetime.now(timezone.utc).isoformat()
    return MemoryRecord(
        id=id,
        content=content,
        summary=None,
        type=type,
        tags=tags or [],
        created_at=now,
        updated_at=now,
        last_accessed=last_accessed or now,
        access_count=access_count,
        importance=importance,
        tier=tier,
        project_dir=project_dir,
        source_session=None,
        supersedes=None,
        consolidated_from=[],
        metadata={},
    )


def _dummy_embedding(dim: int = 384) -> list[float]:
    """Return a trivial embedding vector for insertion."""
    return [0.1] * dim


# ---------------------------------------------------------------------------
# insert_memory + get_memory round-trip
# ---------------------------------------------------------------------------


class TestInsertAndGetMemory:
    """Verify that a memory can be inserted and retrieved intact."""

    def test_roundtrip(self, db_conn: sqlite3.Connection) -> None:
        record: MemoryRecord = _make_record(id="rt-001", content="Round-trip test")
        insert_memory(db_conn, record, _dummy_embedding())

        fetched: MemoryRecord | None = get_memory(db_conn, "rt-001")
        assert fetched is not None
        assert fetched.id == "rt-001"
        assert fetched.content == "Round-trip test"
        assert fetched.type == "user"
        assert fetched.importance == 5.0
        assert fetched.tier == "hot"

    def test_get_missing_returns_none(self, db_conn: sqlite3.Connection) -> None:
        result: MemoryRecord | None = get_memory(db_conn, "nonexistent")
        assert result is None

    def test_tags_roundtrip(self, db_conn: sqlite3.Connection) -> None:
        record: MemoryRecord = _make_record(
            id="rt-tags", tags=["python", "testing"],
        )
        insert_memory(db_conn, record, _dummy_embedding())

        fetched: MemoryRecord | None = get_memory(db_conn, "rt-tags")
        assert fetched is not None
        assert fetched.tags == ["python", "testing"]

    def test_vec_table_populated(self, db_conn: sqlite3.Connection) -> None:
        """Insertion should write to both memories and memories_vec."""
        record: MemoryRecord = _make_record(id="rt-vec")
        emb: list[float] = _dummy_embedding()
        insert_memory(db_conn, record, emb)

        row = db_conn.execute(
            "SELECT embedding FROM memories_vec WHERE id = ?", ("rt-vec",)
        ).fetchone()
        assert row is not None
        stored: list[float] = json.loads(row["embedding"])
        assert len(stored) == 384


# ---------------------------------------------------------------------------
# update_memory
# ---------------------------------------------------------------------------


class TestUpdateMemory:
    """Partial updates via update_memory."""

    def test_single_field_update(self, db_conn: sqlite3.Connection) -> None:
        record: MemoryRecord = _make_record(id="upd-001")
        insert_memory(db_conn, record, _dummy_embedding())

        update_memory(db_conn, "upd-001", content="Updated content")
        fetched: MemoryRecord | None = get_memory(db_conn, "upd-001")
        assert fetched is not None
        assert fetched.content == "Updated content"

    def test_multiple_fields_update(self, db_conn: sqlite3.Connection) -> None:
        record: MemoryRecord = _make_record(id="upd-002", importance=3.0)
        insert_memory(db_conn, record, _dummy_embedding())

        update_memory(
            db_conn,
            "upd-002",
            importance=8.0,
            tier="warm",
            content="Multi-field update",
        )
        fetched: MemoryRecord | None = get_memory(db_conn, "upd-002")
        assert fetched is not None
        assert fetched.importance == 8.0
        assert fetched.tier == "warm"
        assert fetched.content == "Multi-field update"

    def test_updated_at_is_refreshed(self, db_conn: sqlite3.Connection) -> None:
        """updated_at should always be bumped, even if not explicitly set."""
        record: MemoryRecord = _make_record(id="upd-003")
        insert_memory(db_conn, record, _dummy_embedding())

        original: MemoryRecord | None = get_memory(db_conn, "upd-003")
        assert original is not None
        old_updated: str = original.updated_at

        update_memory(db_conn, "upd-003", content="Trigger timestamp bump")
        updated: MemoryRecord | None = get_memory(db_conn, "upd-003")
        assert updated is not None
        assert updated.updated_at >= old_updated

    def test_json_fields_serialised(self, db_conn: sqlite3.Connection) -> None:
        """tags, consolidated_from, metadata should be JSON-serialised."""
        record: MemoryRecord = _make_record(id="upd-004")
        insert_memory(db_conn, record, _dummy_embedding())

        update_memory(
            db_conn,
            "upd-004",
            tags=["new-tag-1", "new-tag-2"],
            metadata={"key": "value"},
        )
        fetched: MemoryRecord | None = get_memory(db_conn, "upd-004")
        assert fetched is not None
        assert fetched.tags == ["new-tag-1", "new-tag-2"]
        assert fetched.metadata == {"key": "value"}

    def test_no_fields_raises(self, db_conn: sqlite3.Connection) -> None:
        """update_memory with no fields should raise ValueError."""
        with pytest.raises(ValueError, match="at least one field"):
            update_memory(db_conn, "upd-005")


# ---------------------------------------------------------------------------
# delete_memory
# ---------------------------------------------------------------------------


class TestDeleteMemory:
    """Deletion should remove from both memories and memories_vec."""

    def test_delete_removes_from_both_tables(
        self, db_conn: sqlite3.Connection
    ) -> None:
        record: MemoryRecord = _make_record(id="del-001")
        insert_memory(db_conn, record, _dummy_embedding())

        delete_memory(db_conn, "del-001")

        assert get_memory(db_conn, "del-001") is None
        vec_row = db_conn.execute(
            "SELECT id FROM memories_vec WHERE id = ?", ("del-001",)
        ).fetchone()
        assert vec_row is None

    def test_delete_nonexistent_is_noop(
        self, db_conn: sqlite3.Connection
    ) -> None:
        """Deleting a missing ID should not raise."""
        delete_memory(db_conn, "ghost-id")  # should not raise


# ---------------------------------------------------------------------------
# search_fts
# ---------------------------------------------------------------------------


class TestSearchFts:
    """Full-text search via FTS5."""

    def test_matching_query(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        from claude_memory.db.queries import search_fts

        results: list[tuple[str, float]] = search_fts(db_conn, "dark mode")
        ids: list[str] = [r[0] for r in results]
        assert "mem-001" in ids

    def test_non_matching_query(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        from claude_memory.db.queries import search_fts

        results: list[tuple[str, float]] = search_fts(db_conn, "xyznonexistent")
        assert len(results) == 0

    def test_project_dir_filter(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        """When project_dir is set, results should include global + matching."""
        from claude_memory.db.queries import search_fts

        results: list[tuple[str, float]] = search_fts(
            db_conn, "commit messages", project_dir="/home/user/my-project",
        )
        ids: list[str] = [r[0] for r in results]
        # mem-010 has project_dir="/home/user/my-project" and content about commit messages
        if results:
            for result_id in ids:
                rec: MemoryRecord | None = get_memory(db_conn, result_id)
                assert rec is not None
                assert rec.project_dir is None or rec.project_dir == "/home/user/my-project"


# ---------------------------------------------------------------------------
# get_always_load
# ---------------------------------------------------------------------------


class TestGetAlwaysLoad:
    """Retrieve high-importance, non-archived memories."""

    def test_returns_high_importance_only(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        ids: list[str] = get_always_load(db_conn, project_dir=None, importance_threshold=7.0)
        # mem-001 (8.0), mem-002 (7.5), mem-003 (9.0), mem-010 (7.0) are >= 7.0
        # mem-009 is archived so excluded
        for mid in ids:
            rec: MemoryRecord | None = get_memory(db_conn, mid)
            assert rec is not None
            assert rec.importance >= 7.0
            assert rec.tier != "archived"

    def test_excludes_archived(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        ids: list[str] = get_always_load(db_conn, project_dir=None, importance_threshold=0.0)
        for mid in ids:
            rec: MemoryRecord | None = get_memory(db_conn, mid)
            assert rec is not None
            assert rec.tier != "archived"

    def test_project_dir_scoping(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        """With project_dir set, should return global + matching project memories."""
        ids: list[str] = get_always_load(
            db_conn, project_dir="/home/user/my-project", importance_threshold=7.0,
        )
        for mid in ids:
            rec: MemoryRecord | None = get_memory(db_conn, mid)
            assert rec is not None
            assert rec.project_dir is None or rec.project_dir == "/home/user/my-project"


# ---------------------------------------------------------------------------
# update_access
# ---------------------------------------------------------------------------


class TestUpdateAccess:
    """Bump access count and last_accessed timestamp."""

    def test_single_id(self, db_conn: sqlite3.Connection) -> None:
        record: MemoryRecord = _make_record(id="acc-001", access_count=5)
        insert_memory(db_conn, record, _dummy_embedding())

        update_access(db_conn, "acc-001")

        fetched: MemoryRecord | None = get_memory(db_conn, "acc-001")
        assert fetched is not None
        assert fetched.access_count == 6

    def test_multiple_ids(self, db_conn: sqlite3.Connection) -> None:
        insert_memory(db_conn, _make_record(id="acc-002", access_count=0), _dummy_embedding())
        insert_memory(db_conn, _make_record(id="acc-003", access_count=10), _dummy_embedding())

        update_access(db_conn, ["acc-002", "acc-003"])

        r2: MemoryRecord | None = get_memory(db_conn, "acc-002")
        r3: MemoryRecord | None = get_memory(db_conn, "acc-003")
        assert r2 is not None and r2.access_count == 1
        assert r3 is not None and r3.access_count == 11

    def test_empty_list_is_noop(self, db_conn: sqlite3.Connection) -> None:
        """Passing an empty list should not raise."""
        update_access(db_conn, [])


# ---------------------------------------------------------------------------
# get_memories_by_tier
# ---------------------------------------------------------------------------


class TestGetMemoriesByTier:
    """Filter memories by tier."""

    def test_hot_tier(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        hot: list[MemoryRecord] = get_memories_by_tier(db_conn, "hot")
        assert len(hot) > 0
        for rec in hot:
            assert rec.tier == "hot"

    def test_cold_tier(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        cold: list[MemoryRecord] = get_memories_by_tier(db_conn, "cold")
        assert len(cold) == 2  # mem-007 and mem-008
        for rec in cold:
            assert rec.tier == "cold"

    def test_archived_tier(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        archived: list[MemoryRecord] = get_memories_by_tier(db_conn, "archived")
        assert len(archived) == 1  # mem-009
        assert archived[0].id == "mem-009"

    def test_empty_tier(self, db_conn: sqlite3.Connection) -> None:
        """A tier with no members should return an empty list."""
        result: list[MemoryRecord] = get_memories_by_tier(db_conn, "cold")
        assert result == []


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------


class TestGetStats:
    """Aggregate counts by type and tier."""

    def test_stats_with_sample_data(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        stats: dict = get_stats(db_conn)

        assert stats["total"] == 10

        # by_type checks
        assert "user" in stats["by_type"]
        assert stats["by_type"]["user"] == 2  # mem-001, mem-010

        assert "project" in stats["by_type"]
        assert stats["by_type"]["project"] == 3  # mem-002, mem-004, mem-009

        # by_tier checks
        assert stats["by_tier"]["hot"] == 4  # mem-001, mem-002, mem-003, mem-010
        assert stats["by_tier"]["warm"] == 3  # mem-004, mem-005, mem-006
        assert stats["by_tier"]["cold"] == 2  # mem-007, mem-008
        assert stats["by_tier"]["archived"] == 1  # mem-009

    def test_stats_empty_db(self, db_conn: sqlite3.Connection) -> None:
        stats: dict = get_stats(db_conn)
        assert stats["total"] == 0
        assert stats["by_type"] == {}
        assert stats["by_tier"] == {}


# ---------------------------------------------------------------------------
# bulk_update_importance
# ---------------------------------------------------------------------------


class TestBulkUpdateImportance:
    """Apply importance decay to memories not accessed today."""

    def test_decay_applied_to_old_memories(
        self, db_conn: sqlite3.Connection
    ) -> None:
        """Memories last accessed before today should have importance decayed."""
        yesterday: str = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()
        record: MemoryRecord = _make_record(
            id="decay-001",
            importance=8.0,
            last_accessed=yesterday,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        affected: int = bulk_update_importance(db_conn, decay_rate=0.9)

        assert affected >= 1
        fetched: MemoryRecord | None = get_memory(db_conn, "decay-001")
        assert fetched is not None
        assert fetched.importance == pytest.approx(8.0 * 0.9, rel=1e-4)

    def test_no_decay_for_today_accesses(
        self, db_conn: sqlite3.Connection
    ) -> None:
        """Memories accessed today should NOT be decayed."""
        now: str = datetime.now(timezone.utc).isoformat()
        record: MemoryRecord = _make_record(
            id="decay-002",
            importance=8.0,
            last_accessed=now,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        bulk_update_importance(db_conn, decay_rate=0.5)

        fetched: MemoryRecord | None = get_memory(db_conn, "decay-002")
        assert fetched is not None
        assert fetched.importance == pytest.approx(8.0)

    def test_importance_floor(self, db_conn: sqlite3.Connection) -> None:
        """Importance should not drop below 0.1."""
        old: str = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        record: MemoryRecord = _make_record(
            id="decay-003",
            importance=0.1,
            last_accessed=old,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        bulk_update_importance(db_conn, decay_rate=0.1)

        fetched: MemoryRecord | None = get_memory(db_conn, "decay-003")
        assert fetched is not None
        assert fetched.importance >= 0.1

    def test_archived_excluded(self, db_conn: sqlite3.Connection) -> None:
        """Archived memories should not be decayed."""
        old: str = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        record: MemoryRecord = _make_record(
            id="decay-004",
            importance=5.0,
            tier="archived",
            last_accessed=old,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        bulk_update_importance(db_conn, decay_rate=0.5)

        fetched: MemoryRecord | None = get_memory(db_conn, "decay-004")
        assert fetched is not None
        assert fetched.importance == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# update_tiers
# ---------------------------------------------------------------------------


class TestUpdateTiers:
    """Tier promotion and demotion logic."""

    def test_promote_to_hot(self, db_conn: sqlite3.Connection) -> None:
        """A warm memory with enough recent accesses should be promoted to hot."""
        now: str = datetime.now(timezone.utc).isoformat()
        record: MemoryRecord = _make_record(
            id="tier-001",
            tier="warm",
            access_count=10,
            last_accessed=now,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        promoted, _, _ = update_tiers(
            db_conn,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        assert promoted >= 1
        fetched: MemoryRecord | None = get_memory(db_conn, "tier-001")
        assert fetched is not None
        assert fetched.tier == "hot"

    def test_demote_hot_to_warm(self, db_conn: sqlite3.Connection) -> None:
        """A hot memory not accessed recently should be demoted to warm."""
        old: str = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        record: MemoryRecord = _make_record(
            id="tier-002",
            tier="hot",
            access_count=1,
            importance=5.0,
            last_accessed=old,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        _, demoted_warm, _ = update_tiers(
            db_conn,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        assert demoted_warm >= 1
        fetched: MemoryRecord | None = get_memory(db_conn, "tier-002")
        assert fetched is not None
        assert fetched.tier == "warm"

    def test_demote_warm_to_cold(self, db_conn: sqlite3.Connection) -> None:
        """A warm memory stale enough with low importance becomes cold."""
        old: str = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        record: MemoryRecord = _make_record(
            id="tier-003",
            tier="warm",
            access_count=0,
            importance=2.0,
            last_accessed=old,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        _, _, demoted_cold = update_tiers(
            db_conn,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        assert demoted_cold >= 1
        fetched: MemoryRecord | None = get_memory(db_conn, "tier-003")
        assert fetched is not None
        assert fetched.tier == "cold"

    def test_archived_untouched(self, db_conn: sqlite3.Connection) -> None:
        """Archived memories should not be promoted or demoted."""
        old: str = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        record: MemoryRecord = _make_record(
            id="tier-004",
            tier="archived",
            access_count=100,
            importance=9.0,
            last_accessed=old,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        update_tiers(
            db_conn,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        fetched: MemoryRecord | None = get_memory(db_conn, "tier-004")
        assert fetched is not None
        assert fetched.tier == "archived"
