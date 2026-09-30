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

    def test_blank_project_dir_loads_only_global_memories(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        ids = get_always_load(
            db_conn,
            project_dir="   ",
            importance_threshold=7.0,
        )

        for memory_id in ids:
            record = get_memory(db_conn, memory_id)
            assert record is not None
            assert record.project_dir is None

    # -- Bounding (issue #29) ------------------------------------------------
    #
    # Without a LIMIT the importance threshold alone qualifies the majority of
    # a mature corpus (measured: 1,811 of 2,909 live memories), at which point
    # "always load" stops being a priority set and simply floods the candidate
    # pool. These tests pin the bound and the project/global split that keeps
    # it fair.

    def _seed_growing_corpus(
        self,
        conn: sqlite3.Connection,
        encoder: MockEncoder,
        count: int,
        project_dir: str | None = None,
        prefix: str = "grow",
    ) -> None:
        """Insert *count* memories that all clear the always-load threshold.

        Commits before returning: an open write transaction plus a cached
        SELECT makes SQLite refuse the ``create_function`` call that every
        scope-aware query performs (issue #31), so an uncommitted seed would
        fail the *next* read rather than this one.
        """
        for i in range(count):
            record = _make_record(
                id=f"{prefix}-{i:04d}",
                content=f"High-importance memory number {i}",
                importance=7.0 + (i % 30) / 10.0,
                project_dir=project_dir,
            )
            insert_memory(conn, record, encoder.encode(record.content))
        conn.commit()

    def test_unbounded_by_default(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Omitting *limit* keeps the pre-existing return-everything contract."""
        self._seed_growing_corpus(db_conn, mock_encoder, 40)

        assert len(get_always_load(db_conn, project_dir=None)) == 40

    def test_stays_bounded_as_corpus_grows(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """The bounded set must not grow with the corpus — the issue-#29 defect.

        Every seeded memory qualifies as always-load, so an unbounded query
        would return 50, then 150, then 300.
        """
        sizes: list[int] = []
        for _ in range(3):
            self._seed_growing_corpus(
                db_conn, mock_encoder, 50, prefix=f"batch{len(sizes)}",
            )
            sizes.append(len(get_always_load(db_conn, project_dir=None, limit=45)))

        assert sizes == [45, 45, 45]

    def test_limit_returns_the_most_important_first(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """A bound must truncate the weakest end, not an arbitrary one."""
        for i in range(20):
            record = _make_record(
                id=f"imp-{i:02d}",
                content=f"Memory {i}",
                importance=7.0 + i / 10.0,
            )
            insert_memory(db_conn, record, mock_encoder.encode(record.content))

        ids: list[str] = get_always_load(db_conn, project_dir=None, limit=5)

        assert ids == ["imp-19", "imp-18", "imp-17", "imp-16", "imp-15"]

    def test_non_positive_limit_returns_nothing(
        self,
        db_conn: sqlite3.Connection,
        sample_memories: list[MemoryRecord],
    ) -> None:
        assert get_always_load(db_conn, project_dir=None, limit=0) == []

    def test_large_project_cannot_evict_every_global(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Half the bound is reserved for globals when the project is huge.

        Mirrors the measured shape of the live corpus, where one project owns
        1,297 always-load memories against 67 globals — deliberately
        asymmetric, unlike a matched-size split. With equal-sized pools any
        reasonable ranking (including one with the reservation split simply
        deleted) happens to divide the bound evenly, so a symmetric seed
        can't tell a real fairness guarantee apart from a coincidence of
        matching pool sizes. Here the project alone is large enough to fill
        the entire bound on its own — only an explicit reservation floor
        leaves room for any global at all.
        """
        self._seed_growing_corpus(
            db_conn, mock_encoder, 100, project_dir="/projects/big", prefix="proj",
        )
        self._seed_growing_corpus(db_conn, mock_encoder, 5, prefix="glob")

        ids: list[str] = get_always_load(
            db_conn, project_dir="/projects/big", limit=30,
        )
        scoped = [i for i in ids if i.startswith("proj-")]
        globals_ = [i for i in ids if i.startswith("glob-")]

        assert len(ids) == 30
        # All 5 globals survive despite the project pool alone being able to
        # fill the entire bound — this is the reservation floor at work, not
        # a coincidence of pool sizes.
        assert len(globals_) == 5
        assert len(scoped) == 25

    def test_small_project_keeps_all_of_its_own_memories(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """A project below its reservation gives the slack back to globals.

        The reverse crowd-out: a handful of project memories must survive a
        bound they could never fill, and must not leave the rest unused.
        """
        self._seed_growing_corpus(
            db_conn, mock_encoder, 3, project_dir="/projects/small", prefix="proj",
        )
        self._seed_growing_corpus(db_conn, mock_encoder, 100, prefix="glob")

        ids: list[str] = get_always_load(
            db_conn, project_dir="/projects/small", limit=30,
        )

        assert len([i for i in ids if i.startswith("proj-")]) == 3
        assert len(ids) == 30

    def test_bounded_set_still_excludes_archived_and_foreign_scopes(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Bounding must not weaken the tier or scope guarantees."""
        for record in (
            _make_record(id="archived", importance=10.0, tier="archived"),
            _make_record(id="foreign", importance=10.0, project_dir="/projects/other"),
            _make_record(id="mine", importance=9.0, project_dir="/projects/mine"),
            _make_record(id="global", importance=9.0),
        ):
            insert_memory(db_conn, record, mock_encoder.encode(record.content))

        ids: list[str] = get_always_load(
            db_conn, project_dir="/projects/mine", limit=10,
        )

        assert sorted(ids) == ["global", "mine"]

    # -- Tiebreak stability (must not couple to the read feedback loop) -----
    #
    # _update_access_stats bumps last_accessed on every retrieval. Ordering
    # the bounded page by last_accessed as a tiebreaker would make ranking
    # depend on which memories happened to be read recently — exactly the
    # feedback loop this bounded page exists to break. created_at (which
    # never changes after insert) and, as a final tiebreak, id (ULIDs sort
    # monotonically with creation order) replace it.

    def test_tiebreak_is_not_perturbed_by_access_feedback(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Reading a memory must not change its place in a tied ordering."""
        earlier = _make_record(id="earlier", importance=8.0)
        insert_memory(db_conn, earlier, mock_encoder.encode(earlier.content))
        later = _make_record(id="later", importance=8.0)
        insert_memory(db_conn, later, mock_encoder.encode(later.content))
        # Commit before the first scope-aware read: an open write transaction
        # plus a cached SELECT makes SQLite refuse create_function (issue #31).
        db_conn.commit()

        before: list[str] = get_always_load(db_conn, project_dir=None, limit=2)
        assert before == ["later", "earlier"]

        # Simulate "earlier" being retrieved repeatedly — the read-driven
        # signal the tiebreak must not respond to.
        for _ in range(3):
            update_access(db_conn, "earlier")
        db_conn.commit()

        after: list[str] = get_always_load(db_conn, project_dir=None, limit=2)
        assert after == before

    def test_id_is_final_tiebreak_when_created_at_also_ties(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """Two rows tied on both importance and created_at fall back to id."""
        tied_created_at: str = "2026-01-01T00:00:00+00:00"
        for record_id in ("aaa-001", "zzz-002"):
            record = MemoryRecord(
                id=record_id,
                content=f"Content for {record_id}",
                summary=None,
                type="lesson",
                tags=[],
                created_at=tied_created_at,
                updated_at=tied_created_at,
                last_accessed=tied_created_at,
                access_count=0,
                importance=8.0,
                tier="hot",
                project_dir=None,
                source_session=None,
                supersedes=None,
                consolidated_from=[],
                metadata={},
            )
            insert_memory(db_conn, record, mock_encoder.encode(record.content))

        ids: list[str] = get_always_load(db_conn, project_dir=None, limit=2)

        assert ids == ["zzz-002", "aaa-001"]

    def test_membership_is_stable_across_access_updating_recalls(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> None:
        """The bounded page's ID SET must not drift as its members get read.

        This fix owns SET-stability of the always-load truncation — three
        successive recalls, each bumping ``last_accessed`` for whatever the
        prior call returned (exactly what a real retrieval does), must keep
        selecting the same members. It does NOT own RANK-stability of a
        final scored/reranked result: recency is a deliberate scorer term
        there, so a member's position in that output is expected to move —
        that is scorer-domain (claude_memory.retrieval.scorer), not this
        page's contract, and is not asserted here.
        """
        for i in range(6):
            record = _make_record(id=f"tied-{i:02d}", importance=8.0)
            insert_memory(db_conn, record, mock_encoder.encode(record.content))
        db_conn.commit()

        page_1: set[str] = set(get_always_load(db_conn, project_dir=None, limit=3))
        update_access(db_conn, list(page_1))
        db_conn.commit()

        page_2: set[str] = set(get_always_load(db_conn, project_dir=None, limit=3))
        update_access(db_conn, list(page_2))
        db_conn.commit()

        page_3: set[str] = set(get_always_load(db_conn, project_dir=None, limit=3))

        assert page_1 == page_2 == page_3


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
