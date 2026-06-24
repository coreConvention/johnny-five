"""Tests for near-duplicate detection and merge-on-store logic.

The dedup module uses the embedding encoder and vector search to find
near-duplicates.  We mock the encoder and use the brute-force vector
search from conftest so no external C extensions are needed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from claude_memory.db.queries import (
    MemoryRecord,
    get_memory,
    insert_memory,
)
from claude_memory.lifecycle.dedup import (
    DedupResult,
    _merge_content,
    store_with_dedup,
)

from tests.conftest import MockEncoder, brute_force_vec_search


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_and_insert(
    conn: sqlite3.Connection,
    encoder: MockEncoder,
    id: str,
    content: str,
    tags: list[str] | None = None,
    importance: float = 5.0,
) -> MemoryRecord:
    """Insert a memory and return the record."""
    now: str = datetime.now(timezone.utc).isoformat()
    record = MemoryRecord(
        id=id,
        content=content,
        summary=None,
        type="user",
        tags=json.dumps(tags or []),
        created_at=now,
        updated_at=now,
        last_accessed=now,
        access_count=0,
        importance=importance,
        tier="hot",
        project_dir=None,
        source_session=None,
        supersedes=None,
        consolidated_from=json.dumps([]),
        metadata=json.dumps({}),
    )
    embedding: list[float] = encoder.encode(content)
    insert_memory(conn, record, embedding)
    return record


# ---------------------------------------------------------------------------
# _merge_content
# ---------------------------------------------------------------------------


class TestMergeContent:
    """Content merge strategies for short and long content."""

    def test_short_content_concatenated(self) -> None:
        """Content < 500 chars should be concatenated with a separator."""
        result: str = _merge_content("Hello world", "New info")
        assert "Hello world" in result
        assert "New info" in result
        assert "---" in result

    def test_long_content_uses_also_prefix(self) -> None:
        """Content >= 500 chars should use 'Also:' addendum."""
        long_existing: str = "x" * 500
        result: str = _merge_content(long_existing, "New info")
        assert result.startswith(long_existing)
        assert "Also: New info" in result
        assert "---" not in result

    def test_exactly_500_uses_also(self) -> None:
        """Content of exactly 500 chars should use 'Also:' (>= 500 check is < 500)."""
        existing: str = "a" * 500
        result: str = _merge_content(existing, "addition")
        assert "Also: addition" in result


# ---------------------------------------------------------------------------
# store_with_dedup — no duplicate
# ---------------------------------------------------------------------------


class TestStoreNoDuplicate:
    """Storing new content with no near-duplicate should insert a fresh record."""

    def test_new_memory_inserted(
        self, db_conn: sqlite3.Connection, mock_encoder: MockEncoder
    ) -> None:
        with patch(
            "claude_memory.lifecycle.dedup.search_vec",
            side_effect=lambda c, emb, top_k=5: brute_force_vec_search(c, emb, top_k),
        ):
            result: DedupResult = store_with_dedup(
                conn=db_conn,
                encoder=mock_encoder,
                content="Brand new unique content for testing",
                type="user",
                tags=["test"],
                importance=6.0,
            )

        assert result.action == "inserted"
        assert result.merged_with is None
        assert result.memory_id != ""

        # Verify the record exists in the database.
        fetched: MemoryRecord | None = get_memory(db_conn, result.memory_id)
        assert fetched is not None
        assert fetched.content == "Brand new unique content for testing"
        assert fetched.importance == 6.0

    def test_inserted_record_has_correct_tier(
        self, db_conn: sqlite3.Connection, mock_encoder: MockEncoder
    ) -> None:
        """Newly inserted memories should start in the hot tier."""
        with patch(
            "claude_memory.lifecycle.dedup.search_vec",
            side_effect=lambda c, emb, top_k=5: brute_force_vec_search(c, emb, top_k),
        ):
            result: DedupResult = store_with_dedup(
                conn=db_conn,
                encoder=mock_encoder,
                content="Another unique memory",
                type="project",
            )

        fetched: MemoryRecord | None = get_memory(db_conn, result.memory_id)
        assert fetched is not None
        assert fetched.tier == "hot"


# ---------------------------------------------------------------------------
# store_with_dedup — near-duplicate detected
# ---------------------------------------------------------------------------


class TestStoreMergesDuplicate:
    """When a near-duplicate is found, the existing memory should be merged."""

    def _setup_duplicate_scenario(
        self,
        db_conn: sqlite3.Connection,
        mock_encoder: MockEncoder,
    ) -> str:
        """Insert an initial memory and return its id.

        We use a mock search_vec that returns the existing memory with a
        distance below the dedup threshold to simulate a near-duplicate.
        """
        record: MemoryRecord = _make_and_insert(
            db_conn, mock_encoder,
            id="existing-001",
            content="The user prefers dark mode",
            tags=["preferences"],
            importance=5.0,
        )
        return record.id

    def test_merge_action(
        self, db_conn: sqlite3.Connection, mock_encoder: MockEncoder
    ) -> None:
        existing_id: str = self._setup_duplicate_scenario(db_conn, mock_encoder)

        # Patch search_vec to return the existing memory as a near-duplicate.
        def fake_search_vec(
            conn: sqlite3.Connection,
            embedding: list[float],
            top_k: int = 5,
        ) -> list[tuple[str, float]]:
            return [(existing_id, 0.05)]  # distance < dedup_threshold (0.15)

        with patch("claude_memory.lifecycle.dedup.search_vec", side_effect=fake_search_vec):
            result: DedupResult = store_with_dedup(
                conn=db_conn,
                encoder=mock_encoder,
                content="User likes dark themes in editors",
                type="user",
                tags=["themes"],
                importance=4.0,
            )

        assert result.action == "merged"
        assert result.merged_with == existing_id
        assert result.memory_id == existing_id

    def test_merged_importance_bumped(
        self, db_conn: sqlite3.Connection, mock_encoder: MockEncoder
    ) -> None:
        """After merge, importance should be bumped by 1.0."""
        existing_id: str = self._setup_duplicate_scenario(db_conn, mock_encoder)

        def fake_search_vec(
            conn: sqlite3.Connection,
            embedding: list[float],
            top_k: int = 5,
        ) -> list[tuple[str, float]]:
            return [(existing_id, 0.05)]

        with patch("claude_memory.lifecycle.dedup.search_vec", side_effect=fake_search_vec):
            store_with_dedup(
                conn=db_conn,
                encoder=mock_encoder,
                content="User likes dark themes",
                type="user",
            )

        fetched: MemoryRecord | None = get_memory(db_conn, existing_id)
        assert fetched is not None
        assert fetched.importance == pytest.approx(6.0)  # 5.0 + 1.0

    def test_importance_capped_at_ten(
        self, db_conn: sqlite3.Connection, mock_encoder: MockEncoder
    ) -> None:
        """Importance should not exceed 10.0 after merge."""
        _make_and_insert(
            db_conn, mock_encoder,
            id="cap-001",
            content="Very important preference",
            importance=9.5,
        )

        def fake_search_vec(
            conn: sqlite3.Connection,
            embedding: list[float],
            top_k: int = 5,
        ) -> list[tuple[str, float]]:
            return [("cap-001", 0.05)]

        with patch("claude_memory.lifecycle.dedup.search_vec", side_effect=fake_search_vec):
            store_with_dedup(
                conn=db_conn,
                encoder=mock_encoder,
                content="Same preference restated",
                type="user",
            )

        fetched: MemoryRecord | None = get_memory(db_conn, "cap-001")
        assert fetched is not None
        assert fetched.importance == pytest.approx(10.0)

    def test_tags_merged_no_duplicates(
        self, db_conn: sqlite3.Connection, mock_encoder: MockEncoder
    ) -> None:
        """Tags should be unioned with no duplicates, preserving order."""
        _make_and_insert(
            db_conn, mock_encoder,
            id="tags-001",
            content="Some memory",
            tags=["a", "b", "c"],
        )

        def fake_search_vec(
            conn: sqlite3.Connection,
            embedding: list[float],
            top_k: int = 5,
        ) -> list[tuple[str, float]]:
            return [("tags-001", 0.05)]

        with patch("claude_memory.lifecycle.dedup.search_vec", side_effect=fake_search_vec):
            store_with_dedup(
                conn=db_conn,
                encoder=mock_encoder,
                content="Related memory",
                type="user",
                tags=["b", "d"],
            )

        fetched: MemoryRecord | None = get_memory(db_conn, "tags-001")
        assert fetched is not None
        # Tags should be the union: ["a", "b", "c", "d"] in order.
        assert fetched.tags == ["a", "b", "c", "d"]

    def test_content_merged(
        self, db_conn: sqlite3.Connection, mock_encoder: MockEncoder
    ) -> None:
        """Merged memory should contain both old and new content."""
        _make_and_insert(
            db_conn, mock_encoder,
            id="merge-content-001",
            content="Original content here",
        )

        def fake_search_vec(
            conn: sqlite3.Connection,
            embedding: list[float],
            top_k: int = 5,
        ) -> list[tuple[str, float]]:
            return [("merge-content-001", 0.05)]

        with patch("claude_memory.lifecycle.dedup.search_vec", side_effect=fake_search_vec):
            store_with_dedup(
                conn=db_conn,
                encoder=mock_encoder,
                content="Additional context",
                type="user",
            )

        fetched: MemoryRecord | None = get_memory(db_conn, "merge-content-001")
        assert fetched is not None
        assert "Original content here" in fetched.content
        assert "Additional context" in fetched.content

    def test_no_merge_above_threshold(
        self, db_conn: sqlite3.Connection, mock_encoder: MockEncoder
    ) -> None:
        """If all candidates are above the dedup threshold, insert new."""
        _make_and_insert(
            db_conn, mock_encoder,
            id="far-001",
            content="Completely different topic",
        )

        def fake_search_vec(
            conn: sqlite3.Connection,
            embedding: list[float],
            top_k: int = 5,
        ) -> list[tuple[str, float]]:
            return [("far-001", 0.90)]  # distance > dedup_threshold

        with patch("claude_memory.lifecycle.dedup.search_vec", side_effect=fake_search_vec):
            result: DedupResult = store_with_dedup(
                conn=db_conn,
                encoder=mock_encoder,
                content="Unrelated new content",
                type="user",
            )

        assert result.action == "inserted"
        assert result.merged_with is None
