"""Candidate merging and multi-signal reranking.

This module sits between raw search results (vector, FTS5, always-load) and
the final ranked output.  It:

1. **Merges** candidates from three heterogeneous sources into a unified
   :class:`RetrievalCandidate` list, deduplicating by memory id.
2. **Reranks** candidates using the multi-signal scorer, applying tier-aware
   semantic-similarity thresholds so that colder memories must be highly
   relevant to surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from claude_memory.retrieval.scorer import (
    ScoredCandidate,
    ScoringWeights,
    compute_combined_score,
)

if TYPE_CHECKING:
    from claude_memory.db.queries import MemoryRecord


# ---------------------------------------------------------------------------
# Default tier thresholds — minimum semantic similarity required for a memory
# in each tier to be considered as a candidate.
# ---------------------------------------------------------------------------

_DEFAULT_TIER_THRESHOLDS: dict[str, float] = {
    "hot": 0.0,   # hot memories are always considered
    "warm": 0.75,
    "cold": 0.90,
}


@dataclass(slots=True)
class RetrievalCandidate:
    """A raw candidate from one or more search backends before scoring.

    Attributes
    ----------
    memory_id:
        The unique id of the memory.
    vec_distance:
        Cosine distance from vector search (lower = more similar).  ``None``
        when the candidate did not come from vector search.
    fts_rank:
        FTS5 rank score (lower = better match).  ``None`` when the candidate
        did not come from full-text search.
    is_always_load:
        ``True`` if this memory is flagged for unconditional loading (e.g.
        user preferences, project settings).
    """

    memory_id: str
    vec_distance: float | None = None
    fts_rank: float | None = None
    is_always_load: bool = False


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def merge_candidates(
    vec_results: list[tuple[str, float]],
    fts_results: list[tuple[str, float]],
    always_load_ids: list[str],
) -> list[RetrievalCandidate]:
    """Merge candidates from vector search, FTS5, and always-load lists.

    When a memory appears in multiple sources its signals are combined into a
    single :class:`RetrievalCandidate`.  Deduplication is by ``memory_id``.

    Parameters
    ----------
    vec_results:
        ``(memory_id, cosine_distance)`` pairs from the vector index.
    fts_results:
        ``(memory_id, fts_rank)`` pairs from the FTS5 index.
    always_load_ids:
        Memory ids that should always be loaded regardless of relevance.

    Returns
    -------
    list[RetrievalCandidate]
        A deduplicated candidate list ready for reranking.
    """
    candidates: dict[str, RetrievalCandidate] = {}

    for memory_id, distance in vec_results:
        candidate = candidates.setdefault(
            memory_id,
            RetrievalCandidate(memory_id=memory_id),
        )
        candidate.vec_distance = distance

    for memory_id, rank in fts_results:
        candidate = candidates.setdefault(
            memory_id,
            RetrievalCandidate(memory_id=memory_id),
        )
        candidate.fts_rank = rank

    for memory_id in always_load_ids:
        candidate = candidates.setdefault(
            memory_id,
            RetrievalCandidate(memory_id=memory_id),
        )
        candidate.is_always_load = True

    return list(candidates.values())


# ---------------------------------------------------------------------------
# Reranking
# ---------------------------------------------------------------------------

def _semantic_similarity_from_candidate(candidate: RetrievalCandidate) -> float:
    """Convert cosine distance to similarity, defaulting to 0.0 when absent.

    Cosine distance is ``1 - cosine_similarity``, so:
        ``similarity = 1.0 - distance``

    If the candidate has no vector distance (e.g. it came only from FTS or
    the always-load set), we return 0.0 and rely on other signals.
    """
    if candidate.vec_distance is not None:
        return max(0.0, min(1.0 - candidate.vec_distance, 1.0))
    return 0.0


def _days_since(dt: datetime | str | None) -> float:
    """Return the number of days between *dt* and now (UTC).

    Accepts a :class:`datetime`, an ISO-8601 string, or ``None``.
    Returns 0.0 if *dt* is ``None`` (treat as "just now").
    """
    if dt is None:
        return 0.0
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return 0.0
    now = datetime.now(timezone.utc)
    # Ensure the stored datetime is timezone-aware; assume UTC if naive.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    return max(delta.total_seconds() / 86_400.0, 0.0)


def rerank(
    candidates: list[RetrievalCandidate],
    memory_records: dict[str, MemoryRecord],
    weights: ScoringWeights = ScoringWeights(),
    top_k: int = 15,
    tier_thresholds: dict[str, float] | None = None,
) -> list[ScoredCandidate]:
    """Score, filter, and rank retrieval candidates.

    Applies **tier-aware filtering** so that memories in colder tiers must
    exceed a higher semantic-similarity bar:

    - ``hot``: no threshold — always considered.
    - ``warm``: semantic similarity must exceed 0.75.
    - ``cold``: semantic similarity must exceed 0.90.
    - ``always_load``: bypasses all thresholds unconditionally.

    Thresholds are customisable via *tier_thresholds*.

    Parameters
    ----------
    candidates:
        Merged candidates from :func:`merge_candidates`.
    memory_records:
        A mapping of ``memory_id → MemoryRecord`` looked up from the
        database.  Candidates whose id is not present here are silently
        skipped (the memory may have been deleted between search and
        rerank).
    weights:
        Multi-signal scoring weights.
    top_k:
        Maximum number of results to return.
    tier_thresholds:
        Override the default minimum semantic-similarity thresholds per
        tier.  Keys are tier names (``"hot"``, ``"warm"``, ``"cold"``);
        values are floats in [0, 1].

    Returns
    -------
    list[ScoredCandidate]
        Up to *top_k* candidates sorted by descending composite score.
    """
    thresholds: dict[str, float] = (
        tier_thresholds if tier_thresholds is not None else _DEFAULT_TIER_THRESHOLDS
    )

    scored: list[ScoredCandidate] = []

    for candidate in candidates:
        record = memory_records.get(candidate.memory_id)
        if record is None:
            # Memory was deleted after the search indices returned it.
            continue

        semantic_sim: float = _semantic_similarity_from_candidate(candidate)

        # --- Tier-aware filtering -------------------------------------------
        if not candidate.is_always_load:
            tier: str = getattr(record, "tier", "hot")
            min_similarity: float = thresholds.get(tier, 0.0)
            if semantic_sim < min_similarity:
                continue

        # --- Score -----------------------------------------------------------
        days: float = _days_since(getattr(record, "last_accessed", None))
        access_count: int = getattr(record, "access_count", 0)
        importance: float = getattr(record, "importance", 5.0)

        sc = compute_combined_score(
            semantic_similarity=semantic_sim,
            days_since_access=days,
            access_count=access_count,
            importance=importance,
            weights=weights,
            memory_id=candidate.memory_id,
        )
        scored.append(sc)

    # Sort descending by composite score, then by semantic as tie-breaker.
    scored.sort(key=lambda s: (s.score, s.semantic_score), reverse=True)

    return scored[:top_k]
