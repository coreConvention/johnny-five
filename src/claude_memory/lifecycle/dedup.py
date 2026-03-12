"""Near-duplicate detection and merge-on-store for memories."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from ulid import ULID

from claude_memory.db.queries import (
    MemoryRecord,
    get_memory,
    insert_memory,
    search_vec,
    update_memory,
)
from claude_memory.embeddings.encoder import EmbeddingEncoder


@dataclass
class DedupResult:
    """Outcome of a :func:`store_with_dedup` call."""

    action: str  # "inserted" | "merged"
    memory_id: str
    merged_with: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _merge_content(existing: str, new: str) -> str:
    """Merge *new* content into *existing*, keeping the result readable.

    If the existing content is short (< 500 chars) we simply concatenate
    with a visual separator.  For longer existing content we append a
    brief "Also:" addendum so the main body stays compact.
    """
    if len(existing) < 500:
        return f"{existing}\n\n---\n\n{new}"
    return f"{existing}\n\nAlso: {new}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def store_with_dedup(
    conn: sqlite3.Connection,
    encoder: EmbeddingEncoder,
    content: str,
    type: str,
    tags: list[str] | None = None,
    importance: float = 5.0,
    project_dir: str | None = None,
    source_session: str | None = None,
    metadata: dict | None = None,
    dedup_threshold: float = 0.15,
) -> DedupResult:
    """Store a memory with near-duplicate detection.

    Workflow
    -------
    1. Generate an embedding for *content*.
    2. Search for near-duplicates whose cosine distance is below
       *dedup_threshold*.
    3. **If a duplicate is found** — merge the new content into the
       existing memory, bump importance by 1.0 (capped at 10.0),
       re-embed the merged text, and update the record in-place.
    4. **If no duplicate** — generate a new ULID and insert a fresh
       record.

    Parameters
    ----------
    conn:
        Open SQLite connection (sqlite-vec extension must be loaded).
    encoder:
        An :class:`EmbeddingEncoder` used to produce embedding vectors.
    content:
        The textual content of the memory to store.
    type:
        Memory type — one of ``user``, ``feedback``, ``project``,
        ``reference``, ``lesson``.
    tags:
        Optional list of string tags for categorisation.
    importance:
        Initial importance score (0.0–10.0, default 5.0).
    project_dir:
        Optional project directory this memory belongs to.
    source_session:
        Optional identifier for the Claude session that produced this
        memory.
    metadata:
        Arbitrary JSON-serialisable metadata dict.
    dedup_threshold:
        Maximum cosine distance (1 − similarity) to consider two
        memories as near-duplicates.  Lower values are stricter.

    Returns
    -------
    DedupResult
        Describes whether a new record was inserted or an existing one
        was merged.
    """
    embedding: list[float] = encoder.encode(content)

    # -- Step 1: look for near-duplicates via vector search ----------------
    candidates: list[tuple[str, float]] = search_vec(
        conn, embedding, top_k=5,
    )

    for candidate_id, distance in candidates:
        if distance >= dedup_threshold:
            # Results are ordered by distance; once we pass the
            # threshold the rest will be even further away.
            break

        existing: MemoryRecord | None = get_memory(conn, candidate_id)
        if existing is None:
            continue

        # -- Merge into existing record ------------------------------------
        merged_content: str = _merge_content(existing.content, content)
        merged_importance: float = min(existing.importance + 1.0, 10.0)
        merged_embedding: list[float] = encoder.encode(merged_content)

        now: str = datetime.now(timezone.utc).isoformat()

        # Merge tags (union of existing and new, preserving order).
        existing_tags: list[str] = (
            json.loads(existing.tags) if isinstance(existing.tags, str) else (existing.tags or [])
        )
        new_tags: list[str] = tags or []
        merged_tags: list[str] = list(dict.fromkeys(existing_tags + new_tags))

        # Merge metadata.
        existing_meta: dict = (
            json.loads(existing.metadata) if isinstance(existing.metadata, str) else (existing.metadata or {})
        )
        merged_meta: dict = {**existing_meta, **(metadata or {})}

        update_memory(
            conn,
            existing.id,
            content=merged_content,
            importance=merged_importance,
            tags=merged_tags,
            metadata=merged_meta,
            tier="hot",
        )
        # Re-embed the merged content in the vector table.
        conn.execute(
            "UPDATE memories_vec SET embedding = ? WHERE id = ?",
            (json.dumps(merged_embedding), existing.id),
        )

        return DedupResult(
            action="merged",
            memory_id=existing.id,
            merged_with=existing.id,
        )

    # -- Step 2: no duplicate — insert a new record ------------------------
    memory_id: str = str(ULID())
    now: str = datetime.now(timezone.utc).isoformat()

    record = MemoryRecord(
        id=memory_id,
        content=content,
        summary=None,
        type=type,
        tags=tags or [],
        created_at=now,
        updated_at=now,
        last_accessed=now,
        access_count=0,
        importance=importance,
        tier="hot",
        project_dir=project_dir,
        source_session=source_session,
        supersedes=None,
        consolidated_from=[],
        metadata=metadata or {},
    )

    insert_memory(conn, record, embedding)

    return DedupResult(action="inserted", memory_id=memory_id)
