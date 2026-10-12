"""Near-duplicate detection and merge-on-store for memories."""

from __future__ import annotations

import json
import re
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
from claude_memory.scope import is_exact_scope_match


@dataclass
class DedupResult:
    """Outcome of a :func:`store_with_dedup` call."""

    action: str  # "inserted" | "merged"
    memory_id: str
    merged_with: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Separates a record's first version from text later merged into it. It is
# distinctive on purpose: a bare "---" is also a markdown rule inside stored
# text, and trimming must never mistake part of the first version for an old
# addition. Records merged before this marker existed (with "---" or "Also:")
# hold no marker, so their whole text counts as the first version and is kept.
_MERGE_MARKER: str = "\n\n--- merged update ---\n\n"

_WHITESPACE: re.Pattern[str] = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Collapse whitespace so containment ignores formatting-only differences."""
    return _WHITESPACE.sub(" ", text).strip()


def _merge_content(existing: str, new: str, max_chars: int) -> str:
    """Merge *new* content into *existing*, keeping the result bounded.

    - *new* already contained in *existing* (identical or a substring): the
      record is returned unchanged, so a recurring identical store is a no-op.
    - *existing* contained in *new*: *new* already says everything the record
      did, so it replaces it instead of repeating it.
    - Otherwise *new* is appended after :data:`_MERGE_MARKER`. The first version
      is always kept and the newest addition always survives; earlier additions
      are dropped oldest-first once the total would exceed *max_chars*.

    The result is at most ``max(max_chars, len(first version) +
    len(_MERGE_MARKER) + len(new))`` characters, however many merges happen.
    """
    normalized_existing: str = _normalize(existing)
    normalized_new: str = _normalize(new)
    if normalized_new in normalized_existing:
        return existing
    if normalized_existing in normalized_new:
        return new

    first, *additions = existing.split(_MERGE_MARKER)
    kept: list[str] = [new]
    budget: int = max_chars - len(first) - len(_MERGE_MARKER) - len(new)
    for addition in reversed(additions):
        cost: int = len(_MERGE_MARKER) + len(addition)
        if cost > budget:
            break
        kept.insert(0, addition)
        budget -= cost
    return _MERGE_MARKER.join([first, *kept])


def _find_near_duplicate(
    conn: sqlite3.Connection,
    embedding: list[float],
    project_dir: str | None,
    dedup_threshold: float,
) -> MemoryRecord | None:
    """Find the nearest compatible duplicate without foreign-candidate starvation."""
    candidate_limit: int = 5
    while True:
        candidates: list[tuple[str, float]] = search_vec(
            conn,
            embedding,
            top_k=candidate_limit,
        )

        for candidate_id, distance in candidates:
            if distance >= dedup_threshold:
                return None

            existing: MemoryRecord | None = get_memory(conn, candidate_id)
            if existing is None:
                continue
            if is_exact_scope_match(existing.project_dir, project_dir):
                return existing

        if len(candidates) < candidate_limit:
            return None

        # All returned candidates were near-duplicates in incompatible scopes.
        # Expand until the ordered result set reaches the threshold or is exhausted.
        candidate_limit *= 2


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
    merge_max_chars: int = 4000,
) -> DedupResult:
    """Store a memory with near-duplicate detection.

    Workflow
    -------
    1. Generate an embedding for *content*.
    2. Search for near-duplicates in the same exact project scope whose cosine
       distance is below *dedup_threshold*.
    3. **If a duplicate is found** — merge the new content into the
       existing memory (see :func:`_merge_content`), union its tags and
       metadata, and update the record in-place.  Only when the merge
       changed the text is importance bumped by 1.0 (capped at 10.0) and
       the merged text re-embedded; text the record already contains
       leaves both untouched.
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
    merge_max_chars:
        Size limit for a merged record.  Past it, the oldest merged
        additions are dropped; the first version and the newest addition
        are always kept.

    Returns
    -------
    DedupResult
        Describes whether a new record was inserted or an existing one
        was merged.
    """
    embedding: list[float] = encoder.encode(content)

    # -- Step 1: look for near-duplicates via vector search ----------------
    existing: MemoryRecord | None = _find_near_duplicate(
        conn,
        embedding,
        project_dir,
        dedup_threshold,
    )

    if existing is not None:
        # -- Merge into existing record ------------------------------------
        merged_content: str = _merge_content(existing.content, content, merge_max_chars)
        content_changed: bool = merged_content != existing.content

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

        fields: dict[str, object] = {
            "tags": merged_tags,
            "metadata": merged_meta,
            "tier": "hot",
        }
        if content_changed:
            # Only new text earns the bump: a scheduled writer repeating itself
            # would otherwise climb to 10 in five runs (issue #45).
            fields["content"] = merged_content
            fields["importance"] = min(existing.importance + 1.0, 10.0)
        update_memory(conn, existing.id, **fields)

        if content_changed:
            # Re-embed the merged content in the vector table.
            merged_embedding: list[float] = encoder.encode(merged_content)
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
