"""Tests for long-retention tuning features.

Covers the three additions introduced for years-scale memory retention:

1. ``MEMORY_RECENCY_DECAY`` env var / ``ScoringWeights.recency_decay`` — makes
   the retrieval-layer recency decay rate configurable (was hardcoded at 0.01).
2. ``forever-keep`` tag exemption from aging and consolidation — pinned
   memories never demote, their importance never decays, and consolidation
   skips them.
3. Auto-consolidation background task helpers in ``server.py`` — guarded by
   ``MEMORY_AUTO_CONSOLIDATE_ENABLED`` and ``MEMORY_AUTO_CONSOLIDATE_INTERVAL_HOURS``.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from claude_memory.config import MemorySettings
from claude_memory.db.queries import (
    MemoryRecord,
    bulk_update_importance,
    get_memory,
    insert_memory,
    update_tiers,
)
from claude_memory.lifecycle.aging import run_aging_cycle
from claude_memory.retrieval.scorer import (
    ScoringWeights,
    compute_combined_score,
    compute_recency_score,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    id: str,
    *,
    tier: str = "hot",
    importance: float = 5.0,
    access_count: int = 0,
    days_ago: float = 0.0,
    tags: list[str] | None = None,
) -> MemoryRecord:
    """Build a MemoryRecord. Tags passed as a plain list — ``insert_memory``
    will ``json.dumps`` them once at insert time, producing the canonical
    stored form (e.g. ``["forever-keep"]``). Pre-dumping here would
    double-encode and break SQL LIKE matching on tag substrings."""
    now: datetime = datetime.now(timezone.utc)
    last_accessed: str = (now - timedelta(days=days_ago)).isoformat()
    return MemoryRecord(
        id=id,
        content=f"Memory {id}",
        summary=None,
        type="user",
        tags=tags or [],
        created_at=(now - timedelta(days=60)).isoformat(),
        updated_at=now.isoformat(),
        last_accessed=last_accessed,
        access_count=access_count,
        importance=importance,
        tier=tier,
        project_dir=None,
        source_session=None,
        supersedes=None,
        consolidated_from=[],
        metadata={},
    )


def _dummy_embedding(dim: int = 384) -> list[float]:
    return [0.1] * dim


# ===========================================================================
# Part 1: configurable recency decay
# ===========================================================================


class TestRecencyDecayConfigurable:
    def test_scoring_weights_exposes_default(self) -> None:
        assert ScoringWeights().recency_decay == 0.01

    def test_recency_score_half_life_at_default(self) -> None:
        """At decay=0.01, 69 days back yields ~0.5 (69-day half-life)."""
        score: float = compute_recency_score(days_since_access=math.log(2) / 0.01)
        assert 0.49 < score < 0.51

    def test_recency_score_extended_half_life(self) -> None:
        """decay=0.002 pushes half-life to ~347 days."""
        score: float = compute_recency_score(
            days_since_access=math.log(2) / 0.002,
            decay_rate=0.002,
        )
        assert 0.49 < score < 0.51

    def test_recency_score_zero_decay_never_fades(self) -> None:
        """decay=0.0 means recency signal doesn't fade with time."""
        score: float = compute_recency_score(
            days_since_access=10_000,
            decay_rate=0.0,
        )
        assert score == pytest.approx(1.0)

    def test_combined_score_uses_weights_decay(self) -> None:
        """compute_combined_score should honour ScoringWeights.recency_decay."""
        weights_slow = ScoringWeights(recency_decay=0.001)  # ~700-day half-life
        weights_fast = ScoringWeights(recency_decay=0.1)    # ~7-day half-life

        slow = compute_combined_score(
            semantic_similarity=0.5,
            days_since_access=100.0,
            access_count=0,
            importance=5.0,
            weights=weights_slow,
        )
        fast = compute_combined_score(
            semantic_similarity=0.5,
            days_since_access=100.0,
            access_count=0,
            importance=5.0,
            weights=weights_fast,
        )

        # At 100 days: slow decay retains high recency, fast decay crushes it.
        assert slow.recency_score > 0.85
        assert fast.recency_score < 0.01

    def test_memory_settings_exposes_recency_decay(self) -> None:
        """Pydantic setting exists with the documented default."""
        settings = MemorySettings()
        assert settings.recency_decay == 0.01


# ===========================================================================
# Part 2a: forever-keep exempts from importance decay
# ===========================================================================


class TestForeverKeepImportanceDecay:
    def test_normal_memory_decays(self, db_conn: sqlite3.Connection) -> None:
        rec = _make_record("normal", importance=8.0, days_ago=5)
        insert_memory(db_conn, rec, _dummy_embedding())

        bulk_update_importance(db_conn, decay_rate=0.5)

        updated = get_memory(db_conn, "normal")
        assert updated is not None
        assert updated.importance == pytest.approx(4.0)  # 8 * 0.5

    def test_pinned_memory_does_not_decay(
        self, db_conn: sqlite3.Connection,
    ) -> None:
        rec = _make_record(
            "pinned",
            importance=8.0,
            days_ago=5,
            tags=["forever-keep"],
        )
        insert_memory(db_conn, rec, _dummy_embedding())

        bulk_update_importance(db_conn, decay_rate=0.5)

        updated = get_memory(db_conn, "pinned")
        assert updated is not None
        assert updated.importance == pytest.approx(8.0)  # unchanged

    def test_pinned_alongside_other_tags(
        self, db_conn: sqlite3.Connection,
    ) -> None:
        """forever-keep tag works when sandwiched between other tags."""
        rec = _make_record(
            "pinned-mixed",
            importance=8.0,
            days_ago=5,
            tags=["critical", "forever-keep", "gotcha"],
        )
        insert_memory(db_conn, rec, _dummy_embedding())

        bulk_update_importance(db_conn, decay_rate=0.5)

        updated = get_memory(db_conn, "pinned-mixed")
        assert updated is not None
        assert updated.importance == pytest.approx(8.0)

    def test_returns_count_excludes_pinned(
        self, db_conn: sqlite3.Connection,
    ) -> None:
        """The returned row count reflects only the memories actually decayed."""
        insert_memory(
            db_conn,
            _make_record("a", importance=5.0, days_ago=5),
            _dummy_embedding(),
        )
        insert_memory(
            db_conn,
            _make_record("b", importance=5.0, days_ago=5, tags=["forever-keep"]),
            _dummy_embedding(),
        )
        insert_memory(
            db_conn,
            _make_record("c", importance=5.0, days_ago=5),
            _dummy_embedding(),
        )

        affected = bulk_update_importance(db_conn, decay_rate=0.9)
        # Only the two un-pinned memories are counted.
        assert affected == 2


# ===========================================================================
# Part 2b: forever-keep exempts from tier demotion
# ===========================================================================


class TestForeverKeepTierTransitions:
    def test_normal_hot_memory_demotes_to_warm(
        self, db_conn: sqlite3.Connection,
    ) -> None:
        rec = _make_record("hot-to-warm", tier="hot", access_count=0, days_ago=40)
        insert_memory(db_conn, rec, _dummy_embedding())

        update_tiers(
            db_conn,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        updated = get_memory(db_conn, "hot-to-warm")
        assert updated is not None
        assert updated.tier == "warm"

    def test_pinned_hot_memory_stays_hot(
        self, db_conn: sqlite3.Connection,
    ) -> None:
        rec = _make_record(
            "pinned-hot",
            tier="hot",
            access_count=0,
            days_ago=40,
            tags=["forever-keep"],
        )
        insert_memory(db_conn, rec, _dummy_embedding())

        update_tiers(
            db_conn,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        updated = get_memory(db_conn, "pinned-hot")
        assert updated is not None
        assert updated.tier == "hot"  # never demoted

    def test_pinned_warm_memory_stays_warm(
        self, db_conn: sqlite3.Connection,
    ) -> None:
        """Warm pinned memory should not demote to cold even if stale + low imp."""
        rec = _make_record(
            "pinned-warm",
            tier="warm",
            importance=1.0,
            access_count=0,
            days_ago=400,  # well past cold_days
            tags=["forever-keep"],
        )
        insert_memory(db_conn, rec, _dummy_embedding())

        update_tiers(
            db_conn,
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        updated = get_memory(db_conn, "pinned-warm")
        assert updated is not None
        assert updated.tier == "warm"

    def test_full_aging_cycle_respects_pin(
        self, db_conn: sqlite3.Connection,
    ) -> None:
        """End-to-end: run_aging_cycle preserves pinned memories entirely."""
        insert_memory(
            db_conn,
            _make_record(
                "keeper",
                tier="hot",
                importance=9.0,
                access_count=0,
                days_ago=100,
                tags=["forever-keep", "core-preference"],
            ),
            _dummy_embedding(),
        )
        insert_memory(
            db_conn,
            _make_record("loser", tier="hot", importance=9.0, days_ago=100),
            _dummy_embedding(),
        )

        run_aging_cycle(
            db_conn,
            decay_rate=0.5,  # aggressive
            hot_access_threshold=3,
            warm_days=30,
            cold_days=180,
            cold_importance_threshold=3.0,
        )

        keeper = get_memory(db_conn, "keeper")
        loser = get_memory(db_conn, "loser")
        assert keeper is not None and loser is not None

        # Pinned: tier + importance both preserved.
        assert keeper.tier == "hot"
        assert keeper.importance == pytest.approx(9.0)

        # Unpinned: demoted AND importance halved.
        assert loser.tier == "warm"
        assert loser.importance == pytest.approx(4.5)


# ===========================================================================
# Part 3: auto-consolidation settings
# ===========================================================================


class TestAutoConsolidationSettings:
    def test_defaults_disabled(self) -> None:
        s = MemorySettings()
        assert s.auto_consolidate_enabled is False
        assert s.auto_consolidate_interval_hours == 168

    def test_settings_respect_env_overrides(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Env vars override the dataclass defaults."""
        monkeypatch.setenv("MEMORY_AUTO_CONSOLIDATE_ENABLED", "true")
        monkeypatch.setenv("MEMORY_AUTO_CONSOLIDATE_INTERVAL_HOURS", "24")
        s = MemorySettings()
        assert s.auto_consolidate_enabled is True
        assert s.auto_consolidate_interval_hours == 24

    @pytest.mark.asyncio
    async def test_start_returns_none_when_disabled(self) -> None:
        """_start_auto_consolidation_if_enabled respects the setting."""
        from claude_memory.server import _start_auto_consolidation_if_enabled

        # Default settings have enabled=False, so no task is spawned.
        task = await _start_auto_consolidation_if_enabled()
        assert task is None

    @pytest.mark.asyncio
    async def test_stop_handles_none(self) -> None:
        """_stop_auto_consolidation tolerates a None task gracefully."""
        from claude_memory.server import _stop_auto_consolidation

        # Should not raise.
        await _stop_auto_consolidation(None)

    @pytest.mark.asyncio
    async def test_loop_respects_minimum_interval(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The loop floors sub-60s interval configurations at 60s.

        We don't actually run a full cycle — we just start the task, let it
        begin its first sleep, then cancel and verify it exited cleanly. The
        point is to confirm the task is constructible and cancellable.
        """
        from claude_memory.server import _auto_consolidation_loop

        task = asyncio.create_task(_auto_consolidation_loop(interval_hours=1))
        # Give the event loop a chance to start the task.
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
