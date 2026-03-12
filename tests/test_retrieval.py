"""Tests for the retrieval pipeline — candidate merging and reranking.

Exercises merge_candidates deduplication and the rerank function with
tier-aware filtering, score sorting, and top_k limiting.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from claude_memory.db.queries import MemoryRecord
from claude_memory.retrieval.reranker import (
    RetrievalCandidate,
    merge_candidates,
    rerank,
)
from claude_memory.retrieval.scorer import ScoredCandidate, ScoringWeights


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory_record(
    id: str,
    tier: str = "hot",
    importance: float = 5.0,
    access_count: int = 5,
    days_ago: float = 0.0,
) -> MemoryRecord:
    """Create a MemoryRecord for testing (not inserted into any database)."""
    now: datetime = datetime.now(timezone.utc)
    last_accessed: str = (now - timedelta(days=days_ago)).isoformat()
    return MemoryRecord(
        id=id,
        content=f"Content for {id}",
        summary=None,
        type="user",
        tags=json.dumps([]),
        created_at=(now - timedelta(days=30)).isoformat(),
        updated_at=now.isoformat(),
        last_accessed=last_accessed,
        access_count=access_count,
        importance=importance,
        tier=tier,
        project_dir=None,
        source_session=None,
        supersedes=None,
        consolidated_from=json.dumps([]),
        metadata=json.dumps({}),
    )


# ---------------------------------------------------------------------------
# merge_candidates
# ---------------------------------------------------------------------------


class TestMergeCandidates:
    """Merge candidates from vec, FTS, and always-load sources."""

    def test_deduplicates_across_sources(self) -> None:
        """A memory appearing in all three sources should produce one candidate."""
        vec: list[tuple[str, float]] = [("m1", 0.2), ("m2", 0.3)]
        fts: list[tuple[str, float]] = [("m1", -1.5), ("m3", -0.8)]
        always: list[str] = ["m1"]

        result: list[RetrievalCandidate] = merge_candidates(vec, fts, always)
        ids: list[str] = [c.memory_id for c in result]

        # Three unique IDs: m1, m2, m3.
        assert sorted(ids) == ["m1", "m2", "m3"]

    def test_preserves_vec_signal(self) -> None:
        """The vec_distance should be set from vec_results."""
        vec: list[tuple[str, float]] = [("m1", 0.25)]
        result: list[RetrievalCandidate] = merge_candidates(vec, [], [])

        assert len(result) == 1
        assert result[0].vec_distance == pytest.approx(0.25)
        assert result[0].fts_rank is None

    def test_preserves_fts_signal(self) -> None:
        """The fts_rank should be set from fts_results."""
        fts: list[tuple[str, float]] = [("m1", -2.0)]
        result: list[RetrievalCandidate] = merge_candidates([], fts, [])

        assert len(result) == 1
        assert result[0].fts_rank == pytest.approx(-2.0)
        assert result[0].vec_distance is None

    def test_preserves_always_load_flag(self) -> None:
        """Always-load IDs should have is_always_load=True."""
        result: list[RetrievalCandidate] = merge_candidates([], [], ["m1"])

        assert len(result) == 1
        assert result[0].is_always_load is True

    def test_combined_signals(self) -> None:
        """A memory in both vec and fts should carry both signals."""
        vec: list[tuple[str, float]] = [("m1", 0.1)]
        fts: list[tuple[str, float]] = [("m1", -3.0)]

        result: list[RetrievalCandidate] = merge_candidates(vec, fts, [])

        assert len(result) == 1
        c: RetrievalCandidate = result[0]
        assert c.vec_distance == pytest.approx(0.1)
        assert c.fts_rank == pytest.approx(-3.0)
        assert c.is_always_load is False

    def test_all_signals_combined(self) -> None:
        """A memory appearing in all three sources should have all signals."""
        vec: list[tuple[str, float]] = [("m1", 0.05)]
        fts: list[tuple[str, float]] = [("m1", -5.0)]
        always: list[str] = ["m1"]

        result: list[RetrievalCandidate] = merge_candidates(vec, fts, always)

        assert len(result) == 1
        c: RetrievalCandidate = result[0]
        assert c.vec_distance == pytest.approx(0.05)
        assert c.fts_rank == pytest.approx(-5.0)
        assert c.is_always_load is True

    def test_empty_inputs(self) -> None:
        """All empty inputs should produce an empty list."""
        result: list[RetrievalCandidate] = merge_candidates([], [], [])
        assert result == []


# ---------------------------------------------------------------------------
# rerank — tier-aware filtering
# ---------------------------------------------------------------------------


class TestRerankTierFiltering:
    """Verify that warm/cold memories need higher semantic similarity."""

    def test_hot_memory_always_considered(self) -> None:
        """Hot memories should pass through regardless of semantic similarity."""
        candidate = RetrievalCandidate(memory_id="hot1", vec_distance=0.8)
        records: dict[str, MemoryRecord] = {
            "hot1": _make_memory_record("hot1", tier="hot"),
        }

        scored: list[ScoredCandidate] = rerank([candidate], records)
        assert len(scored) == 1
        assert scored[0].memory_id == "hot1"

    def test_warm_memory_below_threshold_filtered(self) -> None:
        """Warm memories with semantic similarity < 0.75 should be filtered out."""
        # vec_distance=0.5 → similarity=0.5, which is below warm threshold of 0.75
        candidate = RetrievalCandidate(memory_id="warm1", vec_distance=0.5)
        records: dict[str, MemoryRecord] = {
            "warm1": _make_memory_record("warm1", tier="warm"),
        }

        scored: list[ScoredCandidate] = rerank([candidate], records)
        assert len(scored) == 0

    def test_warm_memory_above_threshold_passes(self) -> None:
        """Warm memories with semantic similarity >= 0.75 should pass."""
        # vec_distance=0.2 → similarity=0.8, above warm threshold of 0.75
        candidate = RetrievalCandidate(memory_id="warm2", vec_distance=0.2)
        records: dict[str, MemoryRecord] = {
            "warm2": _make_memory_record("warm2", tier="warm"),
        }

        scored: list[ScoredCandidate] = rerank([candidate], records)
        assert len(scored) == 1
        assert scored[0].memory_id == "warm2"

    def test_cold_memory_needs_high_similarity(self) -> None:
        """Cold memories need semantic similarity >= 0.90 to pass."""
        # vec_distance=0.15 → similarity=0.85, below cold threshold of 0.90
        cold_below = RetrievalCandidate(memory_id="cold_low", vec_distance=0.15)
        # vec_distance=0.05 → similarity=0.95, above cold threshold
        cold_above = RetrievalCandidate(memory_id="cold_high", vec_distance=0.05)

        records: dict[str, MemoryRecord] = {
            "cold_low": _make_memory_record("cold_low", tier="cold"),
            "cold_high": _make_memory_record("cold_high", tier="cold"),
        }

        scored: list[ScoredCandidate] = rerank(
            [cold_below, cold_above], records
        )
        ids: list[str] = [s.memory_id for s in scored]
        assert "cold_high" in ids
        assert "cold_low" not in ids

    def test_always_load_bypasses_thresholds(self) -> None:
        """Always-load candidates bypass tier thresholds entirely."""
        # Warm memory with low similarity, but flagged as always_load.
        candidate = RetrievalCandidate(
            memory_id="bypass1",
            vec_distance=0.9,  # similarity=0.1, very low
            is_always_load=True,
        )
        records: dict[str, MemoryRecord] = {
            "bypass1": _make_memory_record("bypass1", tier="warm"),
        }

        scored: list[ScoredCandidate] = rerank([candidate], records)
        assert len(scored) == 1
        assert scored[0].memory_id == "bypass1"

    def test_custom_tier_thresholds(self) -> None:
        """Custom thresholds should override the defaults."""
        candidate = RetrievalCandidate(memory_id="custom1", vec_distance=0.4)
        records: dict[str, MemoryRecord] = {
            "custom1": _make_memory_record("custom1", tier="warm"),
        }

        # Default warm threshold is 0.75. Similarity=0.6 would be filtered.
        # But with a custom threshold of 0.5, it should pass.
        scored: list[ScoredCandidate] = rerank(
            [candidate], records,
            tier_thresholds={"hot": 0.0, "warm": 0.5, "cold": 0.9},
        )
        assert len(scored) == 1


# ---------------------------------------------------------------------------
# rerank — sorting and top_k
# ---------------------------------------------------------------------------


class TestRerankSortingAndLimit:
    """Verify sorting by composite score and top_k limit."""

    def test_sorted_by_score_descending(self) -> None:
        """Results should be sorted by composite score, highest first."""
        candidates: list[RetrievalCandidate] = [
            RetrievalCandidate(memory_id="low", vec_distance=0.9),
            RetrievalCandidate(memory_id="high", vec_distance=0.05),
            RetrievalCandidate(memory_id="mid", vec_distance=0.5),
        ]
        records: dict[str, MemoryRecord] = {
            "low": _make_memory_record("low", tier="hot", importance=2.0),
            "high": _make_memory_record("high", tier="hot", importance=9.0),
            "mid": _make_memory_record("mid", tier="hot", importance=5.0),
        }

        scored: list[ScoredCandidate] = rerank(candidates, records)

        # Scores should be in descending order.
        for i in range(len(scored) - 1):
            assert scored[i].score >= scored[i + 1].score

        # The highest-similarity, highest-importance should be first.
        assert scored[0].memory_id == "high"

    def test_top_k_limit(self) -> None:
        """rerank should return at most top_k results."""
        candidates: list[RetrievalCandidate] = [
            RetrievalCandidate(memory_id=f"m{i}", vec_distance=0.1 * i)
            for i in range(10)
        ]
        records: dict[str, MemoryRecord] = {
            f"m{i}": _make_memory_record(f"m{i}", tier="hot")
            for i in range(10)
        }

        scored: list[ScoredCandidate] = rerank(
            candidates, records, top_k=3
        )
        assert len(scored) <= 3

    def test_missing_record_skipped(self) -> None:
        """Candidates with no matching record should be silently skipped."""
        candidate = RetrievalCandidate(memory_id="ghost", vec_distance=0.1)
        records: dict[str, MemoryRecord] = {}  # empty — no match

        scored: list[ScoredCandidate] = rerank([candidate], records)
        assert len(scored) == 0

    def test_no_vec_distance_defaults_to_zero_similarity(self) -> None:
        """A candidate with no vec_distance should have semantic_score=0.0."""
        candidate = RetrievalCandidate(memory_id="fts_only", fts_rank=-3.0)
        records: dict[str, MemoryRecord] = {
            "fts_only": _make_memory_record("fts_only", tier="hot"),
        }

        scored: list[ScoredCandidate] = rerank([candidate], records)
        assert len(scored) == 1
        assert scored[0].semantic_score == pytest.approx(0.0)
