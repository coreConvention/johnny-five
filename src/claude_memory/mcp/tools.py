"""MCP tool definitions for claude-memory.

Each tool is an async function that manages its own database connection
and returns a plain dict suitable for JSON serialization.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from claude_memory.config import MemorySettings, get_settings
from claude_memory.db.connection import get_connection
from claude_memory.db.queries import (
    MemoryRecord,
    delete_memory,
    get_memory,
    get_stats,
    update_memory,
)
from claude_memory.embeddings.encoder import EmbeddingEncoder, get_encoder
from claude_memory.lifecycle.aging import AgingReport, run_aging_cycle
from claude_memory.lifecycle.consolidation import ConsolidationReport, run_consolidation
from claude_memory.lifecycle.dedup import DedupResult, store_with_dedup
from claude_memory.retrieval.scorer import ScoringWeights
from claude_memory.retrieval.search import (
    SearchResult,
    recall_session_memories,
    search_memories,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_deps() -> tuple[sqlite3.Connection, EmbeddingEncoder, MemorySettings]:
    """Return a (connection, encoder, settings) triple.

    Each tool call creates its own connection so that concurrent requests
    cannot interfere with each other.
    """
    settings: MemorySettings = get_settings()
    conn: sqlite3.Connection = get_connection(
        settings.resolve_db_path(), settings.embedding_dim,
    )
    encoder: EmbeddingEncoder = get_encoder(settings.model_name)
    return conn, encoder, settings


def _weights_from_settings(settings: MemorySettings) -> ScoringWeights:
    """Build :class:`ScoringWeights` from the current settings.

    Includes ``kappa`` (keyword-boost weight). Production default is 0.30;
    set ``MEMORY_KAPPA=0`` to fall back to pure semantic-only ranking.
    """
    return ScoringWeights(
        alpha=settings.alpha,
        beta=settings.beta,
        gamma=settings.gamma,
        delta=settings.delta,
        kappa=settings.kappa,
    )


def _search_result_to_dict(result: SearchResult) -> dict:
    """Serialize a :class:`SearchResult` into a JSON-friendly dict."""
    return {
        "id": result.memory.id,
        "content": result.memory.content,
        "type": result.memory.type,
        "tags": result.memory.tags if isinstance(result.memory.tags, list) else json.loads(result.memory.tags or "[]"),
        "importance": result.memory.importance,
        "tier": result.memory.tier,
        "score": round(result.score, 4),
        "semantic_score": round(result.semantic_score, 4),
        "recency_score": round(result.recency_score, 4),
        "frequency_score": round(result.frequency_score, 4),
        "importance_score": round(result.importance_score, 4),
        "lexical_score": round(result.lexical_score, 4),
        "project_dir": result.memory.project_dir,
        "created_at": result.memory.created_at,
        "updated_at": result.memory.updated_at,
        "access_count": result.memory.access_count,
    }


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


async def tool_memory_store(
    content: str,
    type: str,
    tags: list[str] | None = None,
    importance: float = 5.0,
    project_dir: str | None = None,
    source_session: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Store a new memory with dedup detection.

    Parameters
    ----------
    content:
        The memory content to store.
    type:
        Memory type -- one of ``user``, ``feedback``, ``project``,
        ``reference``, ``lesson``.
    tags:
        Optional categorization tags.
    importance:
        Importance score (0.0--10.0, default 5.0).
    project_dir:
        Optional project directory scope.
    source_session:
        Optional session identifier.
    metadata:
        Optional arbitrary metadata dict.
    """
    conn, encoder, settings = _get_deps()
    try:
        result: DedupResult = store_with_dedup(
            conn=conn,
            encoder=encoder,
            content=content,
            type=type,
            tags=tags,
            importance=importance,
            project_dir=project_dir,
            source_session=source_session,
            metadata=metadata,
            dedup_threshold=settings.dedup_threshold,
        )
        conn.commit()
        return {
            "action": result.action,
            "memory_id": result.memory_id,
            "merged_with": result.merged_with,
        }
    finally:
        conn.close()


async def tool_memory_search(
    query: str,
    project_dir: str | None = None,
    top_k: int | None = None,
    token_budget: int | None = None,
) -> dict:
    """Search memories using hybrid multi-signal retrieval.

    Combines semantic (vector) + lexical (FTS5 + keyword-overlap boost) +
    recency + frequency + importance signals. When ``MEMORY_KAPPA > 0``
    (default 0.30) memories matching the query's keywords get boosted
    above paraphrase-adjacent neighbours.

    Parameters
    ----------
    query:
        Natural-language search query. Also drives the keyword-overlap boost.
    project_dir:
        Optional project directory to scope results.
    top_k:
        Maximum number of results before the token-budget cut.
    token_budget:
        Optional cap on cumulative token cost of returned ``content``. Useful
        for fitting recall output into a hook-injected context block.
        Top-1 result is always returned even if it alone exceeds the budget.
    """
    conn, encoder, settings = _get_deps()
    try:
        effective_top_k: int = top_k if top_k is not None else settings.top_k
        weights: ScoringWeights = _weights_from_settings(settings)

        results: list[SearchResult] = search_memories(
            conn=conn,
            encoder=encoder,
            query=query,
            project_dir=project_dir,
            weights=weights,
            top_k=effective_top_k,
            token_budget=token_budget,
        )
        conn.commit()
        return {
            "results": [_search_result_to_dict(r) for r in results],
        }
    finally:
        conn.close()


async def tool_memory_recall(
    project_dir: str | None = None,
    initial_context: str = "",
    top_k: int | None = None,
    token_budget: int | None = None,
) -> dict:
    """Session-start recall of relevant memories.

    Loads high-importance always-load memories unconditionally, plus
    semantically-and-lexically relevant memories if *initial_context* is
    provided.  When ``MEMORY_KAPPA > 0`` (default 0.30) memories that
    literally mention the initial_context's keywords rank higher than
    paraphrase-adjacent neighbours.

    Parameters
    ----------
    project_dir:
        Optional project directory to scope results.
    initial_context:
        Free-text context for semantic+lexical bootstrapping. An empty
        string skips the optional ranked search but still loads always-load.
    top_k:
        Maximum number of results before the token-budget cut.
    token_budget:
        Optional cap on cumulative token cost of returned ``content``.
        Recommended default for session-start hook injection: 1500.
        Top-1 result is always returned even if it alone exceeds the budget.
    """
    conn, encoder, settings = _get_deps()
    try:
        effective_top_k: int = top_k if top_k is not None else settings.top_k
        weights: ScoringWeights = _weights_from_settings(settings)

        results: list[SearchResult] = recall_session_memories(
            conn=conn,
            encoder=encoder,
            project_dir=project_dir,
            initial_context=initial_context,
            weights=weights,
            top_k=effective_top_k,
            token_budget=token_budget,
        )
        conn.commit()
        return {
            "results": [_search_result_to_dict(r) for r in results],
        }
    finally:
        conn.close()


async def tool_memory_update(
    memory_id: str,
    content: str | None = None,
    importance: float | None = None,
    tags: list[str] | None = None,
    type: str | None = None,
) -> dict:
    """Update an existing memory.

    Only the provided fields are modified; all others remain unchanged.
    If *content* is updated, the embedding is also re-computed.

    Parameters
    ----------
    memory_id:
        The ID of the memory to update.
    content:
        New content text (triggers re-embedding).
    importance:
        New importance score (0.0--10.0).
    tags:
        New tag list (replaces existing tags).
    type:
        New memory type.
    """
    conn, encoder, settings = _get_deps()
    try:
        # Verify the memory exists.
        existing: MemoryRecord | None = get_memory(conn, memory_id)
        if existing is None:
            return {"updated": False, "memory_id": memory_id, "error": "Memory not found"}

        # Collect fields to update.
        fields: dict = {}
        if content is not None:
            fields["content"] = content
        if importance is not None:
            fields["importance"] = importance
        if tags is not None:
            fields["tags"] = tags
        if type is not None:
            fields["type"] = type

        if not fields:
            return {"updated": False, "memory_id": memory_id, "error": "No fields to update"}

        update_memory(conn, memory_id, **fields)

        # Re-embed if content changed.
        if content is not None:
            new_embedding: list[float] = encoder.encode(content)
            conn.execute(
                "UPDATE memories_vec SET embedding = ? WHERE id = ?",
                (json.dumps(new_embedding), memory_id),
            )

        conn.commit()
        return {"updated": True, "memory_id": memory_id}
    finally:
        conn.close()


async def tool_memory_forget(
    memory_id: str,
    archive: bool = True,
) -> dict:
    """Archive or permanently delete a memory.

    Parameters
    ----------
    memory_id:
        The ID of the memory to forget.
    archive:
        If ``True`` (default), the memory is moved to the ``archived``
        tier and remains in the database.  If ``False``, the memory is
        permanently deleted.
    """
    conn, _, settings = _get_deps()
    try:
        existing: MemoryRecord | None = get_memory(conn, memory_id)
        if existing is None:
            return {"action": "not_found", "memory_id": memory_id, "error": "Memory not found"}

        if archive:
            update_memory(conn, memory_id, tier="archived")
            action = "archived"
        else:
            delete_memory(conn, memory_id)
            action = "deleted"

        conn.commit()
        return {"action": action, "memory_id": memory_id}
    finally:
        conn.close()


async def tool_memory_consolidate() -> dict:
    """Trigger manual consolidation of cold-tier memories.

    Clusters semantically similar cold memories, generates summaries,
    and archives the originals.
    """
    conn, encoder, settings = _get_deps()
    try:
        report: ConsolidationReport = run_consolidation(
            conn=conn,
            encoder=encoder,
        )
        conn.commit()
        return asdict(report)
    finally:
        conn.close()


async def tool_memory_stats() -> dict:
    """Return database statistics.

    Returns counts grouped by memory type, tier, and total.
    """
    conn, _, settings = _get_deps()
    try:
        return get_stats(conn)
    finally:
        conn.close()


async def tool_memory_aging() -> dict:
    """Run an aging cycle (importance decay + tier updates).

    Applies daily importance decay to all non-archived memories that
    haven't been accessed recently, then re-evaluates tier placement
    for every non-archived memory.
    """
    conn, _, settings = _get_deps()
    try:
        report: AgingReport = run_aging_cycle(
            conn=conn,
            decay_rate=settings.decay_rate,
            hot_access_threshold=settings.hot_access_threshold,
            warm_days=settings.warm_days,
            cold_days=settings.cold_days,
            cold_importance_threshold=settings.cold_importance_threshold,
        )
        conn.commit()
        return asdict(report)
    finally:
        conn.close()
