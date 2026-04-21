"""High-level search orchestrator for the multi-signal retrieval engine.

Provides two entry points:

- :func:`search_memories` — ad-hoc query search combining vector, FTS, and
  always-load candidates through multi-signal scoring.
- :func:`recall_session_memories` — session-start recall that loads user
  preferences, project context, and optionally performs a semantic search
  against an initial context string.
"""

from __future__ import annotations

import functools
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
    lexical_score: float = 0.0


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
                lexical_score=sc.lexical_score,
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
# Token-budget truncation
#
# Callers (notably session-start-recall hooks) often have a tight context
# budget.  Rather than always returning ``top_k`` full memories, they can
# pass ``token_budget`` to cap the total content size of the returned list.
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _get_tiktoken_encoding():
    """Return a cached tiktoken encoding, or ``None`` if tiktoken isn't installed.

    Uses ``cl100k_base`` as a reasonable approximation of Claude's BPE
    tokenizer; Anthropic's own tokenizer is proprietary but BPE-family, so
    tiktoken estimates are close enough for budgeting purposes (typically
    within 5-10%).
    """
    try:
        import tiktoken  # local import so tiktoken is an optional dependency
        return tiktoken.get_encoding("cl100k_base")
    except ImportError:
        return None


def _estimate_tokens(text: str) -> int:
    """Estimate the token count of *text*.

    Uses tiktoken's cl100k_base encoding when available, falling back to
    ``ceil(len(text) / 4)`` which tends to overestimate slightly for
    English prose — the safer direction when enforcing a budget.
    """
    if not text:
        return 0
    enc = _get_tiktoken_encoding()
    if enc is not None:
        return len(enc.encode(text))
    return (len(text) + 3) // 4


def _truncate_to_token_budget(
    results: list[SearchResult],
    token_budget: int,
) -> list[SearchResult]:
    """Keep results in rank order until the cumulative token cost exceeds the budget.

    Invariants
    ----------
    - Always includes the top-1 result, even if it alone exceeds the budget
      (the caller explicitly asked for results; returning zero would be
      worse than over-budget-by-one honesty).
    - Counts only ``memory.content`` (the dominant size contributor); scores
      and metadata are a rounding-error fraction of the payload.
    - A non-positive ``token_budget`` disables truncation (returns input).
    """
    if token_budget <= 0 or not results:
        return results
    kept: list[SearchResult] = []
    used: int = 0
    for i, r in enumerate(results):
        cost: int = _estimate_tokens(r.memory.content or "")
        if i == 0 or used + cost <= token_budget:
            kept.append(r)
            used += cost
        else:
            break
    return kept


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
    token_budget: int | None = None,
) -> list[SearchResult]:
    """Full multi-signal retrieval pipeline.

    Steps
    -----
    1. Embed the query text.
    2. Run vector search, FTS search, and always-load query.
    3. Merge and deduplicate candidates.
    4. Look up full :class:`MemoryRecord` for each candidate.
    5. Rerank with multi-signal scoring, tier-aware filtering, and keyword
       overlap (boost driven by ``weights.kappa``).
    6. Optionally truncate to a token budget.
    7. Optionally update access statistics for returned results.
    8. Return the final :class:`SearchResult` list.

    Parameters
    ----------
    conn:
        An open SQLite connection (with sqlite-vec and FTS5 loaded).
    encoder:
        The embedding encoder used to vectorise the query.
    query:
        Natural-language query string.  Also forwarded to :func:`rerank`
        so the keyword-overlap signal can use it.
    project_dir:
        If provided, scopes always-load and FTS results to this project
        directory.
    weights:
        Multi-signal scoring weights.  Set ``weights.kappa > 0`` to enable
        the keyword-overlap boost.
    top_k:
        Maximum number of results to return (before token-budget truncation).
    update_access_on_retrieve:
        When ``True``, bump access count and last-accessed timestamp for
        every returned memory.
    token_budget:
        Optional maximum cumulative token cost of the returned results'
        ``content``.  Results are iterated in ranking order and the list
        is truncated at the first candidate that would exceed the budget
        (top-1 is always included regardless).

    Returns
    -------
    list[SearchResult]
        Up to *top_k* results sorted by descending composite score, further
        truncated by *token_budget* if set.
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

    # 5. Rerank — pass query so keyword-overlap signal can score candidates.
    scored: list[ScoredCandidate] = rerank(
        candidates, records, weights=weights, top_k=top_k, query=query,
    )

    # 6. Build results
    results: list[SearchResult] = _to_search_results(scored, records)

    # 7. Token-budget truncation (before access stats so we only mark what
    #    the caller actually receives)
    if token_budget is not None:
        results = _truncate_to_token_budget(results, token_budget)

    # 8. Update access stats
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
    token_budget: int | None = None,
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

    # Pass initial_context as the "query" so keyword-overlap can boost
    # memories mentioning the user's stated focus.  When initial_context is
    # empty the boost naturally becomes a no-op.
    scored: list[ScoredCandidate] = rerank(
        all_candidates, records, weights=weights, top_k=top_k,
        query=initial_context if initial_context.strip() else None,
    )

    results: list[SearchResult] = _to_search_results(scored, records)

    # Apply token budget BEFORE bumping access stats so we don't inflate
    # access counts on memories the caller never actually receives.
    if token_budget is not None:
        results = _truncate_to_token_budget(results, token_budget)

    # Update access stats for recalled memories.
    if results:
        _update_access_stats(conn, results)

    return results
