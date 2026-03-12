"""Tests for the aging lifecycle module.

Covers importance decay, tier promotion/demotion, and the full aging cycle.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from claude_memory.db.queries import (
    MemoryRecord,
    get_memory,
    insert_memory,
)
from claude_memory.lifecycle.aging import (
    AgingReport,
    run_aging_cycle,
    run_importance_decay,
    run_tier_updates,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    id: str,
    tier: str = "hot",
    importance: float = 5.0,
    access_count: int = 0,
    days_ago: float = 0.0,
) -> MemoryRecord:
    """Build a MemoryRecord with last_accessed set to *days_ago* days in the past."""
    now: datetime = datetime.now(timezone.utc)
    last_accessed: str = (now - timedelta(days=days_ago)).isoformat()
    return MemoryRecord(
        id=id,
        content=f"Memory {id}",
        summary=None,
        type="user",
        tags=json.dumps([]),
        created_at=(now - timedelta(days=60)).isoformat(),
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


def _dummy_embedding(dim: int = 384) -> list[float]:
    return [0.1] * dim


# ---------------------------------------------------------------------------
# run_importance_decay
# ---------------------------------------------------------------------------


class TestRunImportanceDecay:
    """Importance decay: reduce importance for stale memories."""

    def test_decays_old_memories(self, db_conn: sqlite3.Connection) -> None:
        """Memories last accessed before today should have importance decayed."""
        record: MemoryRecord = _make_record("decay-old", importance=8.0, days_ago=5)
        insert_memory(db_conn, record, _dummy_embedding())

        affected: int = run_importance_decay(db_conn, decay_rate=0.9)

        assert affected >= 1
        fetched: MemoryRecord | None = get_memory(db_conn, "decay-old")
        assert fetched is not None
        assert fetched.importance == pytest.approx(8.0 * 0.9, rel=1e-4)

    def test_does_not_decay_recent_memories(
        self, db_conn: sqlite3.Connection
    ) -> None:
        """Memories accessed today should NOT be decayed."""
        record: MemoryRecord = _make_record("decay-recent", importance=8.0, days_ago=0)
        insert_memory(db_conn, record, _dummy_embedding())

        run_importance_decay(db_conn, decay_rate=0.5)

        fetched: MemoryRecord | None = get_memory(db_conn, "decay-recent")
        assert fetched is not None
        assert fetched.importance == pytest.approx(8.0)

    def test_does_not_decay_archived(self, db_conn: sqlite3.Connection) -> None:
        """Archived memories should be excluded from decay."""
        record: MemoryRecord = _make_record(
            "decay-archived", tier="archived", importance=6.0, days_ago=30
        )
        insert_memory(db_conn, record, _dummy_embedding())

        run_importance_decay(db_conn, decay_rate=0.5)

        fetched: MemoryRecord | None = get_memory(db_conn, "decay-archived")
        assert fetched is not None
        assert fetched.importance == pytest.approx(6.0)

    def test_importance_floor_at_0_1(self, db_conn: sqlite3.Connection) -> None:
        """Importance should not drop below 0.1."""
        record: MemoryRecord = _make_record(
            "decay-floor", importance=0.2, days_ago=10
        )
        insert_memory(db_conn, record, _dummy_embedding())

        run_importance_decay(db_conn, decay_rate=0.01)

        fetched: MemoryRecord | None = get_memory(db_conn, "decay-floor")
        assert fetched is not None
        assert fetched.importance >= 0.1

    def test_returns_count_of_affected(self, db_conn: sqlite3.Connection) -> None:
        """Return value should reflect number of decayed memories."""
        for i in range(5):
            record: MemoryRecord = _make_record(
                f"decay-count-{i}", importance=7.0, days_ago=3
            )
            insert_memory(db_conn, record, _dummy_embedding())

        affected: int = run_importance_decay(db_conn, decay_rate=0.99)
        assert affected == 5


# ---------------------------------------------------------------------------
# run_tier_updates — promotion
# ---------------------------------------------------------------------------


class TestTierPromotion:
    """Warm memories with enough recent accesses should be promoted to hot."""

    def test_warm_to_hot(self, db_conn: sqlite3.Connection) -> None:
        """A warm memory with sufficient accesses within the window is promoted."""
        record: MemoryRecord = _make_record(
            "promo-001", tier="warm", access_count=10, days_ago=1
        )
        insert_memory(db_conn, record, _dummy_embedding())

        report: AgingReport = run_tier_updates(
            db_conn,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        assert report.promoted_to_hot >= 1
        fetched: MemoryRecord | None = get_memory(db_conn, "promo-001")
        assert fetched is not None
        assert fetched.tier == "hot"

    def test_warm_not_enough_accesses(self, db_conn: sqlite3.Connection) -> None:
        """A warm memory with too few accesses should stay warm."""
        record: MemoryRecord = _make_record(
            "promo-002", tier="warm", access_count=1, days_ago=1, importance=5.0,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        report: AgingReport = run_tier_updates(
            db_conn,
            hot_access_threshold=5,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        fetched: MemoryRecord | None = get_memory(db_conn, "promo-002")
        assert fetched is not None
        assert fetched.tier == "warm"


# ---------------------------------------------------------------------------
# run_tier_updates — demotion
# ---------------------------------------------------------------------------


class TestTierDemotion:
    """Hot/warm memories should be demoted when stale or low-importance."""

    def test_hot_to_warm(self, db_conn: sqlite3.Connection) -> None:
        """A hot memory not accessed in warm_days should demote to warm."""
        record: MemoryRecord = _make_record(
            "demote-001", tier="hot", access_count=1, days_ago=60, importance=5.0,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        report: AgingReport = run_tier_updates(
            db_conn,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        assert report.demoted_to_warm >= 1
        fetched: MemoryRecord | None = get_memory(db_conn, "demote-001")
        assert fetched is not None
        assert fetched.tier == "warm"

    def test_warm_to_cold(self, db_conn: sqlite3.Connection) -> None:
        """A warm memory stale enough with low importance demotes to cold."""
        record: MemoryRecord = _make_record(
            "demote-002", tier="warm", access_count=0, days_ago=60,
            importance=2.0,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        report: AgingReport = run_tier_updates(
            db_conn,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        assert report.demoted_to_cold >= 1
        fetched: MemoryRecord | None = get_memory(db_conn, "demote-002")
        assert fetched is not None
        assert fetched.tier == "cold"

    def test_warm_high_importance_stays_warm(
        self, db_conn: sqlite3.Connection
    ) -> None:
        """A warm memory with high importance should NOT demote to cold."""
        record: MemoryRecord = _make_record(
            "demote-003", tier="warm", access_count=0, days_ago=60,
            importance=8.0,
        )
        insert_memory(db_conn, record, _dummy_embedding())

        run_tier_updates(
            db_conn,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        fetched: MemoryRecord | None = get_memory(db_conn, "demote-003")
        assert fetched is not None
        assert fetched.tier == "warm"


# ---------------------------------------------------------------------------
# run_aging_cycle — full pipeline
# ---------------------------------------------------------------------------


class TestRunAgingCycle:
    """The full aging cycle does decay + tier updates in sequence."""

    def test_full_cycle_returns_report(
        self, db_conn: sqlite3.Connection
    ) -> None:
        """run_aging_cycle should return a complete AgingReport."""
        # Insert a mix of memories at various ages and tiers.
        insert_memory(
            db_conn,
            _make_record("cycle-001", tier="hot", importance=8.0, days_ago=0, access_count=20),
            _dummy_embedding(),
        )
        insert_memory(
            db_conn,
            _make_record("cycle-002", tier="hot", importance=5.0, days_ago=60, access_count=1),
            _dummy_embedding(),
        )
        insert_memory(
            db_conn,
            _make_record("cycle-003", tier="warm", importance=2.0, days_ago=60, access_count=0),
            _dummy_embedding(),
        )

        report: AgingReport = run_aging_cycle(
            db_conn,
            decay_rate=0.9,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        assert isinstance(report, AgingReport)
        # cycle-002 and cycle-003 should have been decayed (last_accessed > 1 day ago).
        assert report.memories_decayed >= 2

    def test_decay_runs_before_tier_updates(
        self, db_conn: sqlite3.Connection
    ) -> None:
        """Decay should lower importance before tier updates check thresholds.

        A warm memory with importance=3.1 (just above cold threshold of 3.0)
        should be decayed below 3.0, and then demoted to cold.
        """
        insert_memory(
            db_conn,
            _make_record(
                "order-001", tier="warm", importance=3.1, days_ago=60, access_count=0,
            ),
            _dummy_embedding(),
        )

        report: AgingReport = run_aging_cycle(
            db_conn,
            decay_rate=0.9,  # 3.1 * 0.9 = 2.79, below threshold of 3.0
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        fetched: MemoryRecord | None = get_memory(db_conn, "order-001")
        assert fetched is not None
        # After decay: importance ≈ 2.79 (below 3.0) and stale > 30 days
        # → should be demoted to cold.
        assert fetched.tier == "cold"
        assert fetched.importance < 3.0

    def test_empty_db_returns_zeros(self, db_conn: sqlite3.Connection) -> None:
        """Running an aging cycle on an empty database should not fail."""
        report: AgingReport = run_aging_cycle(db_conn)
        assert report.memories_decayed == 0
        assert report.promoted_to_hot == 0
        assert report.demoted_to_warm == 0
        assert report.demoted_to_cold == 0
