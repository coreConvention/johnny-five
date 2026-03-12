"""High-level search orchestrator for the multi-signal retrieval engine.

Provides two entry points:

- :func:`search_memories` — ad-hoc query search combining vector, FTS, and
  always-load candidates through multi-signal scoring.
- :func:`recall_session_memories` — session-start recall that loads user
  preferences, project context, and optionally performs a semantic search
  against an initial context string.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from claude_memory.db.queries import (
    MemoryRecord,
    get_always_load,
    get_memory,
    search_fts,
    search_vec,
    update_access,
)
from claude_memory.embeddings.encoder import EmbeddingEncoder
from claude_memory.retrieval.reranker import (
    RetrievalCandidate,
    merge_candidates,
    rerank,
)
from claude_memory.retrieval.scorer import ScoredCandidate, ScoringWeights


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Final search result pairing a :class:`MemoryRecord` with its scores.

    Returned by :func:`search_memories` and :func:`recall_session_memories`.
    """

    memory: MemoryRecord
    score: float
    semantic_score: float
    recency_score: float
    frequency_score: float
    importance_score: float


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _lookup_records(
    conn: sqlite3.Connection,
    memory_ids: list[str],
) -> dict[str, MemoryRecord]:
    """Batch-fetch :class:`MemoryRecord` objects by id.

    Returns a dict keyed by memory_id.  Missing ids are silently omitted
    (the memory may have been deleted between search and lookup).
    """
    records: dict[str, MemoryRecord] = {}
    for mid in memory_ids:
        record: MemoryRecord | None = get_memory(conn, mid)
        if record is not None:
            records[mid] = record
    return records


def _to_search_results(
    scored: list[ScoredCandidate],
    records: dict[str, MemoryRecord],
) -> list[SearchResult]:
    """Map :class:`ScoredCandidate` objects to :class:`SearchResult` objects.

    Candidates whose memory_id is not in *records* are dropped (race
    condition guard).
    """
    results: list[SearchResult] = []
    for sc in scored:
        record = records.get(sc.memory_id)
        if record is None:
            continue
        results.append(
            SearchResult(
                memory=record,
                score=sc.score,
                semantic_score=sc.semantic_score,
                recency_score=sc.recency_score,
                frequency_score=sc.frequency_score,
                importance_score=sc.importance_score,
            )
        )
    return results


def _update_access_stats(
    conn: sqlite3.Connection,
    results: list[SearchResult],
) -> None:
    """Bump access count and last-accessed timestamp for returned memories."""
    for result in results:
        update_access(conn, result.memory.id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_memories(
    conn: sqlite3.Connection,
    encoder: EmbeddingEncoder,
    query: str,
    project_dir: str | None = None,
    weights: ScoringWeights = ScoringWeights(),
    top_k: int = 15,
    update_access_on_retrieve: bool = True,
) -> list[SearchResult]:
    """Full multi-signal retrieval pipeline.

    Steps
    -----
    1. Embed the query text.
    2. Run vector search, FTS search, and always-load query.
    3. Merge and deduplicate candidates.
    4. Look up full :class:`MemoryRecord` for each candidate.
    5. Rerank with multi-signal scoring and tier-aware filtering.
    6. Optionally update access statistics for returned results.
    7. Return the final :class:`SearchResult` list.

    Parameters
    ----------
    conn:
        An open SQLite connection (with sqlite-vec and FTS5 loaded).
    encoder:
        The embedding encoder used to vectorise the query.
    query:
        Natural-language query string.
    project_dir:
        If provided, scopes always-load and FTS results to this project
        directory.
    weights:
        Multi-signal scoring weights.
    top_k:
        Maximum number of results to return.
    update_access_on_retrieve:
        When ``True``, bump access count and last-accessed timestamp for
        every returned memory.

    Returns
    -------
    list[SearchResult]
        Up to *top_k* results sorted by descending composite score.
    """
    # 1. Embed
    query_embedding: list[float] = encoder.encode(query)

    # 2. Search — SQLite is single-writer, so sequential is fine.
    vec_results: list[tuple[str, float]] = search_vec(
        conn, query_embedding, top_k=top_k * 3,
    )
    fts_results: list[tuple[str, float]] = search_fts(
        conn, query, project_dir=project_dir, top_k=top_k * 3,
    )
    always_load_ids: list[str] = get_always_load(conn, project_dir=project_dir)

    # 3. Merge
    candidates: list[RetrievalCandidate] = merge_candidates(
        vec_results, fts_results, always_load_ids,
    )

    # 4. Lookup
    all_ids: list[str] = [c.memory_id for c in candidates]
    records: dict[str, MemoryRecord] = _lookup_records(conn, all_ids)

    # 5. Rerank
    scored: list[ScoredCandidate] = rerank(
        candidates, records, weights=weights, top_k=top_k,
    )

    # 6. Build results
    results: list[SearchResult] = _to_search_results(scored, records)

    # 7. Update access stats
    if update_access_on_retrieve and results:
        _update_access_stats(conn, results)

    return results


def recall_session_memories(
    conn: sqlite3.Connection,
    encoder: EmbeddingEncoder,
    project_dir: str | None = None,
    initial_context: str = "",
    weights: ScoringWeights = ScoringWeights(),
    top_k: int = 20,
) -> list[SearchResult]:
    """Session-start recall: load baseline context and relevant memories.

    Designed to be called once at the beginning of a conversation to prime
    the assistant with user preferences, project-specific settings, and
    semantically relevant memories.

    Behaviour
    ---------
    - Always-load memories (user prefs, project config) are unconditionally
      included.
    - If *initial_context* is provided (e.g. the user's first message or a
      project summary), a semantic search is performed against it to surface
      additional relevant memories.
    - Results are deduplicated — a memory returned by both always-load and
      semantic search appears only once, with the higher score.

    Parameters
    ----------
    conn:
        An open SQLite connection.
    encoder:
        The embedding encoder.
    project_dir:
        Optional project directory to scope results.
    initial_context:
        Free-text context for semantic bootstrapping.  Pass an empty string
        to skip the semantic search and return only always-load memories.
    weights:
        Multi-signal scoring weights.
    top_k:
        Maximum total results to return (always-load memories count toward
        this limit).

    Returns
    -------
    list[SearchResult]
        Up to *top_k* results sorted by descending composite score.
    """
    # --- Always-load memories (unconditional) --------------------------------
    always_load_ids: list[str] = get_always_load(conn, project_dir=project_dir)

    # Build baseline candidates from always-load set.
    always_candidates: list[RetrievalCandidate] = [
        RetrievalCandidate(memory_id=mid, is_always_load=True)
        for mid in always_load_ids
    ]

    # --- Semantic search (optional) ------------------------------------------
    semantic_candidates: list[RetrievalCandidate] = []
    if initial_context.strip():
        query_embedding: list[float] = encoder.encode(initial_context)

        vec_results: list[tuple[str, float]] = search_vec(
            conn, query_embedding, top_k=top_k * 3,
        )
        fts_results: list[tuple[str, float]] = search_fts(
            conn, initial_context, project_dir=project_dir, top_k=top_k * 3,
        )

        semantic_candidates = merge_candidates(vec_results, fts_results, [])

    # --- Merge both pools, deduplicating by id --------------------------------
    all_candidates: list[RetrievalCandidate] = merge_candidates(
        # Re-encode always-load as vec_results with distance=0 (max similarity)
        # so they get the highest possible semantic score.
        vec_results=[(c.memory_id, 0.0) for c in always_candidates],
        fts_results=[
            (c.memory_id, c.fts_rank)
            for c in semantic_candidates
            if c.fts_rank is not None
        ],
        always_load_ids=always_load_ids,
    )

    # Fold in vec_distance from semantic candidates (merge_candidates above
    # set vec_distance=0.0 for always-load; overwrite only if semantic gave
    # a *closer* distance for a candidate that is also always-load).
    sem_map: dict[str, RetrievalCandidate] = {
        c.memory_id: c for c in semantic_candidates
    }
    for candidate in all_candidates:
        sem = sem_map.get(candidate.memory_id)
        if sem is not None and sem.vec_distance is not None:
            # For always-load items, keep the better (lower) distance.
            if candidate.is_always_load:
                candidate.vec_distance = min(
                    candidate.vec_distance or 0.0,
                    sem.vec_distance,
                )
            else:
                candidate.vec_distance = sem.vec_distance

    # --- Lookup, score, return ------------------------------------------------
    all_ids: list[str] = [c.memory_id for c in all_candidates]
    records: dict[str, MemoryRecord] = _lookup_records(conn, all_ids)

    scored: list[ScoredCandidate] = rerank(
        all_candidates, records, weights=weights, top_k=top_k,
    )

    results: list[SearchResult] = _to_search_results(scored, records)

    # Update access stats for recalled memories.
    if results:
        _update_access_stats(conn, results)

    return results
