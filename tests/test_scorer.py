"""Tests for the multi-signal scoring module.

Covers individual signal functions and the combined scorer with known inputs.
"""

from __future__ import annotations

import math

import pytest

from claude_memory.retrieval.scorer import (
    ScoredCandidate,
    ScoringWeights,
    compute_combined_score,
    compute_frequency_score,
    compute_importance_score,
    compute_recency_score,
)


# ---------------------------------------------------------------------------
# compute_recency_score
# ---------------------------------------------------------------------------


class TestComputeRecencyScore:
    """Exponential decay: score = exp(-decay_rate * days)."""

    def test_zero_days_returns_one(self) -> None:
        """A memory just accessed should score exactly 1.0."""
        assert compute_recency_score(0.0) == pytest.approx(1.0)

    def test_one_day(self) -> None:
        """After 1 day at default decay_rate=0.01 → exp(-0.01)."""
        expected: float = math.exp(-0.01)
        assert compute_recency_score(1.0) == pytest.approx(expected)

    def test_thirty_days(self) -> None:
        expected: float = math.exp(-0.01 * 30)
        assert compute_recency_score(30.0) == pytest.approx(expected)

    def test_seventy_days_near_half_life(self) -> None:
        """Half-life ≈ ln(2)/0.01 ≈ 69.3 days; at 70 days score ≈ 0.4966."""
        expected: float = math.exp(-0.01 * 70)
        assert compute_recency_score(70.0) == pytest.approx(expected, rel=1e-4)

    def test_365_days(self) -> None:
        """After a full year the score should be very small but positive."""
        score: float = compute_recency_score(365.0)
        assert 0.0 < score < 0.05

    def test_negative_days_clamped_to_zero(self) -> None:
        """Negative days should be treated as zero (just accessed)."""
        assert compute_recency_score(-5.0) == pytest.approx(1.0)

    def test_custom_decay_rate(self) -> None:
        """A higher decay rate should give a lower score for the same days."""
        slow: float = compute_recency_score(30.0, decay_rate=0.01)
        fast: float = compute_recency_score(30.0, decay_rate=0.05)
        assert fast < slow


# ---------------------------------------------------------------------------
# compute_frequency_score
# ---------------------------------------------------------------------------


class TestComputeFrequencyScore:
    """log₂(count + 1) / 10, capped at 1.0."""

    def test_zero_accesses(self) -> None:
        """Zero accesses → log₂(1)/10 = 0.0."""
        assert compute_frequency_score(0) == pytest.approx(0.0)

    def test_one_access(self) -> None:
        """1 access → log₂(2)/10 = 0.1."""
        assert compute_frequency_score(1) == pytest.approx(0.1)

    def test_ten_accesses(self) -> None:
        expected: float = math.log2(11) / 10.0
        assert compute_frequency_score(10) == pytest.approx(expected)

    def test_hundred_accesses(self) -> None:
        expected: float = math.log2(101) / 10.0
        assert compute_frequency_score(100) == pytest.approx(expected)

    def test_1023_accesses_saturates(self) -> None:
        """1023 accesses → log₂(1024)/10 = 10/10 = 1.0."""
        assert compute_frequency_score(1023) == pytest.approx(1.0)

    def test_above_1023_capped(self) -> None:
        """Above saturation the score stays at 1.0."""
        assert compute_frequency_score(5000) == pytest.approx(1.0)

    def test_negative_count_treated_as_zero(self) -> None:
        """Negative counts should be clamped to 0."""
        assert compute_frequency_score(-10) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compute_importance_score
# ---------------------------------------------------------------------------


class TestComputeImportanceScore:
    """Normalise 0-10 → 0-1, clamping out-of-range values."""

    def test_zero(self) -> None:
        assert compute_importance_score(0.0) == pytest.approx(0.0)

    def test_five(self) -> None:
        assert compute_importance_score(5.0) == pytest.approx(0.5)

    def test_ten(self) -> None:
        assert compute_importance_score(10.0) == pytest.approx(1.0)

    def test_above_ten_clamped(self) -> None:
        """Values above 10 should be clamped to 1.0."""
        assert compute_importance_score(15.0) == pytest.approx(1.0)

    def test_negative_clamped(self) -> None:
        """Negative values should be clamped to 0.0."""
        assert compute_importance_score(-3.0) == pytest.approx(0.0)

    def test_midrange(self) -> None:
        assert compute_importance_score(7.5) == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# ScoringWeights
# ---------------------------------------------------------------------------


class TestScoringWeights:
    """Verify default weights sum to 1.0."""

    def test_default_weights_sum_to_one(self) -> None:
        w = ScoringWeights()
        total: float = w.alpha + w.beta + w.gamma + w.delta
        assert total == pytest.approx(1.0)

    def test_individual_defaults(self) -> None:
        w = ScoringWeights()
        assert w.alpha == pytest.approx(0.45)
        assert w.beta == pytest.approx(0.20)
        assert w.gamma == pytest.approx(0.10)
        assert w.delta == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# compute_combined_score
# ---------------------------------------------------------------------------


class TestComputeCombinedScore:
    """Verify weighted sum with known inputs."""

    def test_all_ones(self) -> None:
        """Max possible signal values should produce a score of 1.0."""
        result: ScoredCandidate = compute_combined_score(
            semantic_similarity=1.0,
            days_since_access=0.0,    # recency = 1.0
            access_count=1023,         # frequency = 1.0
            importance=10.0,           # importance = 1.0
            memory_id="test-all-ones",
        )
        assert result.score == pytest.approx(1.0)
        assert result.memory_id == "test-all-ones"

    def test_all_zeros(self) -> None:
        """Minimum signal values (except recency at 0 days = 1.0)."""
        result: ScoredCandidate = compute_combined_score(
            semantic_similarity=0.0,
            days_since_access=99999.0,  # recency ≈ 0.0
            access_count=0,              # frequency = 0.0
            importance=0.0,              # importance = 0.0
            memory_id="test-zeros",
        )
        # Only recency contributes (≈0), semantic=0, freq=0, importance=0
        assert result.score < 0.01

    def test_known_weighted_sum(self) -> None:
        """Verify the weighted sum with manually computed signals."""
        weights = ScoringWeights(alpha=0.4, beta=0.3, gamma=0.1, delta=0.2)

        # Semantic = 0.8
        # Recency = exp(-0.01 * 10) ≈ 0.9048
        # Frequency = log₂(11)/10 ≈ 0.3459
        # Importance = 7.0/10 = 0.7
        result: ScoredCandidate = compute_combined_score(
            semantic_similarity=0.8,
            days_since_access=10.0,
            access_count=10,
            importance=7.0,
            weights=weights,
            memory_id="test-known",
        )

        expected_rec: float = math.exp(-0.01 * 10)
        expected_freq: float = math.log2(11) / 10.0
        expected_imp: float = 0.7

        expected_score: float = (
            0.4 * 0.8
            + 0.3 * expected_rec
            + 0.1 * expected_freq
            + 0.2 * expected_imp
        )

        assert result.score == pytest.approx(expected_score, rel=1e-6)
        assert result.semantic_score == pytest.approx(0.8)
        assert result.recency_score == pytest.approx(expected_rec)
        assert result.frequency_score == pytest.approx(expected_freq)
        assert result.importance_score == pytest.approx(expected_imp)

    def test_semantic_similarity_clamped(self) -> None:
        """Semantic similarity > 1.0 should be clamped to 1.0."""
        result: ScoredCandidate = compute_combined_score(
            semantic_similarity=1.5,
            days_since_access=0.0,
            access_count=0,
            importance=5.0,
        )
        assert result.semantic_score == pytest.approx(1.0)

    def test_negative_semantic_clamped(self) -> None:
        """Semantic similarity < 0.0 should be clamped to 0.0."""
        result: ScoredCandidate = compute_combined_score(
            semantic_similarity=-0.5,
            days_since_access=0.0,
            access_count=0,
            importance=5.0,
        )
        assert result.semantic_score == pytest.approx(0.0)

    def test_custom_decay_rate_forwarded(self) -> None:
        """The decay_rate kwarg should be forwarded to compute_recency_score."""
        slow: ScoredCandidate = compute_combined_score(
            semantic_similarity=0.5,
            days_since_access=30.0,
            access_count=5,
            importance=5.0,
            decay_rate=0.01,
        )
        fast: ScoredCandidate = compute_combined_score(
            semantic_similarity=0.5,
            days_since_access=30.0,
            access_count=5,
            importance=5.0,
            decay_rate=0.1,
        )
        assert fast.recency_score < slow.recency_score
