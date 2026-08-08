"""Memory consolidation — clusters cold-tier memories and archives stragglers."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from ulid import ULID

from claude_memory.db.queries import (
    MemoryRecord,
    get_memories_by_tier,
    get_memory,
    get_non_archived_memories,
    insert_memory,
    update_memory,
)
from claude_memory.embeddings.encoder import EmbeddingEncoder


@dataclass
class ConsolidationReport:
    """Summary produced by :func:`run_consolidation`."""

    clusters_found: int
    memories_consolidated: int
    memories_archived: int
    new_summaries_created: int


def _is_pinned(memory: MemoryRecord) -> bool:
    """Return ``True`` if *memory* carries the ``forever-keep`` tag.

    Pinned memories are never merged, archived, or reconciled — their content
    must stay addressable as-is. ``tags`` may be a list or a JSON string.
    """
    tags = memory.tags
    if isinstance(tags, list):
        return "forever-keep" in tags
    if isinstance(tags, str) and tags:
        try:
            return "forever-keep" in json.loads(tags)
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# Cosine similarity helpers
# ---------------------------------------------------------------------------


def _cosine_similarity_matrix(embeddings: np.ndarray) -> np.ndarray:
    """Return an (N, N) pairwise cosine-similarity matrix.

    *embeddings* is expected to be row-normalised (L2-norm = 1) — which
    is the case for vectors produced by :class:`EmbeddingEncoder` — so
    the dot product equals cosine similarity.
    """
    # Normalise defensively in case vectors are not already unit-length.
    norms: np.ndarray = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    normed: np.ndarray = embeddings / norms
    similarity: np.ndarray = normed @ normed.T
    return similarity


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def find_clusters(
    memories: list[MemoryRecord],
    embeddings: dict[str, list[float]],
    similarity_threshold: float = 0.75,
) -> list[list[str]]:
    """Simple greedy clustering of memories by semantic similarity.

    Algorithm
    ---------
    1. Build a pairwise cosine-similarity matrix for all memories.
    2. For each memory, collect all neighbours whose similarity exceeds
       *similarity_threshold*.
    3. Greedily assign each memory to the first existing cluster that
       shares at least one member, or start a new cluster.
    4. Return only clusters of size >= 3.

    Parameters
    ----------
    memories:
        The list of :class:`MemoryRecord` objects to cluster.
    embeddings:
        Mapping from memory ID → embedding vector.  Must contain an
        entry for every memory in *memories*.
    similarity_threshold:
        Minimum cosine similarity for two memories to be considered
        related.

    Returns
    -------
    list[list[str]]
        Each inner list contains memory IDs belonging to one cluster.
        Only clusters with 3 or more members are included.
    """
    if len(memories) < 3:
        return []

    # Build ordered ID list and embedding matrix.
    ids: list[str] = [m.id for m in memories]

    vectors: np.ndarray = np.array(
        [embeddings[mid] for mid in ids], dtype=np.float32,
    )

    sim_matrix: np.ndarray = _cosine_similarity_matrix(vectors)

    # Build adjacency lists (neighbours above threshold).
    neighbours: dict[str, set[str]] = {}
    for i, mid in enumerate(ids):
        neighbours[mid] = set()
        for j, other_id in enumerate(ids):
            if i != j and sim_matrix[i, j] > similarity_threshold:
                neighbours[mid].add(other_id)

    # Greedy clustering.
    assigned: set[str] = set()
    clusters: list[list[str]] = []

    for mid in ids:
        if mid in assigned:
            continue
        if not neighbours[mid]:
            continue

        # Try to find an existing cluster that overlaps.
        placed = False
        for cluster in clusters:
            cluster_set = set(cluster)
            if neighbours[mid] & cluster_set:
                cluster.append(mid)
                assigned.add(mid)
                placed = True
                break

        if not placed:
            # Start a new cluster with this memory and its neighbours.
            new_cluster: list[str] = [mid]
            assigned.add(mid)
            for neighbour_id in neighbours[mid]:
                if neighbour_id not in assigned:
                    new_cluster.append(neighbour_id)
                    assigned.add(neighbour_id)
            clusters.append(new_cluster)

    # Filter to clusters of size >= 3.
    return [c for c in clusters if len(c) >= 3]


# ---------------------------------------------------------------------------
# Summary generation (placeholder — no LLM dependency)
# ---------------------------------------------------------------------------


def generate_summary(memories: list[MemoryRecord]) -> str:
    """Generate a consolidation summary from a cluster of memories.

    Since this module does not depend on an LLM, the summary is built
    mechanically:

    - First line states how many memories were consolidated and the
      dominant type.
    - Subsequent lines list a key point from each source memory (the
      first sentence or first 100 characters, whichever is shorter).

    This function is intentionally simple and can be replaced with an
    LLM-powered summariser in a future iteration.
    """
    # Determine the most common type across the cluster.
    type_counts: Counter[str] = Counter(m.type for m in memories)
    common_type: str = type_counts.most_common(1)[0][0]

    lines: list[str] = [
        f"Consolidated from {len(memories)} memories about {common_type}",
        "",
    ]

    for memory in memories:
        text: str = memory.content.strip()
        # Take the first sentence or first 100 chars.
        dot_idx: int = text.find(".")
        if 0 < dot_idx <= 100:
            point = text[: dot_idx + 1]
        else:
            point = text[:100].rstrip()
            if len(text) > 100:
                point += "..."
        lines.append(f"- {point}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Full consolidation pipeline
# ---------------------------------------------------------------------------


def run_consolidation(
    conn: sqlite3.Connection,
    encoder: EmbeddingEncoder,
    similarity_threshold: float = 0.75,
    min_cluster_size: int = 3,
) -> ConsolidationReport:
    """Run the full consolidation pipeline on cold-tier memories.

    Steps
    -----
    1. Fetch all memories with ``tier='cold'``.
    2. Retrieve their embeddings from the ``memories_vec`` table.
    3. Cluster them by semantic similarity.
    4. For each cluster (size >= *min_cluster_size*):

       a. Generate a summary of the cluster.
       b. Create a new consolidated memory with the majority type,
          ``tier='warm'``, and importance equal to the max across
          the cluster.
       c. Populate ``consolidated_from`` on the new memory.
       d. Move all original cluster members to ``tier='archived'``.

    5. For isolated cold memories (not in any cluster) with
       ``importance < 1.0`` **and** ``access_count < 2``: archive
       them directly.

    Parameters
    ----------
    conn:
        Open SQLite connection (sqlite-vec extension must be loaded).
    encoder:
        The embedding encoder used to produce vectors for new summaries.
    similarity_threshold:
        Cosine similarity threshold for clustering (passed through to
        :func:`find_clusters`).
    min_cluster_size:
        Minimum number of memories per cluster (default 3).

    Returns
    -------
    ConsolidationReport
        Statistics about the consolidation run.
    """
    cold_memories_all: list[MemoryRecord] = get_memories_by_tier(conn, tier="cold")

    # Exclude forever-keep memories from consolidation. Even if they landed
    # in cold (e.g. the tag was added AFTER the memory aged in), we refuse
    # to merge or archive them — their content must stay addressable as-is.
    cold_memories: list[MemoryRecord] = [
        m for m in cold_memories_all if not _is_pinned(m)
    ]

    if not cold_memories:
        return ConsolidationReport(
            clusters_found=0,
            memories_consolidated=0,
            memories_archived=0,
            new_summaries_created=0,
        )

    # -- Fetch embeddings from memories_vec --------------------------------
    embeddings: dict[str, list[float]] = {}
    for memory in cold_memories:
        row = conn.execute(
            "SELECT embedding FROM memories_vec WHERE id = ?",
            (memory.id,),
        ).fetchone()
        if row is not None:
            # sqlite-vec returns bytes; convert to list[float] via numpy.
            raw = row[0] if not isinstance(row, sqlite3.Row) else row["embedding"]
            if isinstance(raw, bytes):
                vec = np.frombuffer(raw, dtype=np.float32).tolist()
            else:
                vec = list(raw)
            embeddings[memory.id] = vec

    # Only cluster memories that have embeddings.
    embeddable_memories: list[MemoryRecord] = [
        m for m in cold_memories if m.id in embeddings
    ]

    clusters: list[list[str]] = find_clusters(
        embeddable_memories,
        embeddings,
        similarity_threshold=similarity_threshold,
    )

    # Track which memory IDs end up in a cluster.
    clustered_ids: set[str] = set()
    for cluster in clusters:
        clustered_ids.update(cluster)

    now: str = datetime.now(timezone.utc).isoformat()
    memories_by_id: dict[str, MemoryRecord] = {m.id: m for m in cold_memories}

    total_consolidated: int = 0
    total_archived: int = 0
    new_summaries: int = 0

    # -- Process clusters --------------------------------------------------
    for cluster_ids in clusters:
        cluster_memories: list[MemoryRecord] = [
            memories_by_id[mid] for mid in cluster_ids if mid in memories_by_id
        ]
        if len(cluster_memories) < min_cluster_size:
            continue

        # Determine majority type.
        type_counts: Counter[str] = Counter(m.type for m in cluster_memories)
        majority_type: str = type_counts.most_common(1)[0][0]

        # Generate summary and embedding.
        summary_text: str = generate_summary(cluster_memories)
        summary_embedding: list[float] = encoder.encode(summary_text)

        # Max importance across the cluster.
        max_importance: float = max(m.importance for m in cluster_memories)

        # Collect all unique tags from cluster members.
        all_tags: list[str] = []
        for m in cluster_memories:
            mem_tags: list[str] = (
                json.loads(m.tags) if isinstance(m.tags, str) else (m.tags or [])
            )
            all_tags.extend(mem_tags)
        merged_tags: list[str] = list(dict.fromkeys(all_tags))

        new_id: str = str(ULID())
        # insert_memory owns JSON encoding of tags/consolidated_from/metadata,
        # so pass native list/dict here — pre-dumping double-encodes them and
        # yields type-unstable rows on read-back (issue #11).
        consolidated_record = MemoryRecord(
            id=new_id,
            content=summary_text,
            summary=None,
            type=majority_type,
            tags=merged_tags,
            created_at=now,
            updated_at=now,
            last_accessed=now,
            access_count=0,
            importance=max_importance,
            tier="warm",
            project_dir=cluster_memories[0].project_dir,
            source_session=None,
            supersedes=None,
            consolidated_from=cluster_ids,
            metadata={},
        )

        insert_memory(conn, consolidated_record, summary_embedding)
        new_summaries += 1
        total_consolidated += len(cluster_memories)

        # Archive original cluster members.
        for mid in cluster_ids:
            update_memory(
                conn,
                mid,
                tier="archived",
            )
            total_archived += 1

    # -- Archive isolated low-value cold memories --------------------------
    for memory in cold_memories:
        if memory.id in clustered_ids:
            continue
        if memory.importance < 1.0 and memory.access_count < 2:
            update_memory(
                conn,
                memory.id,
                tier="archived",
            )
            total_archived += 1

    return ConsolidationReport(
        clusters_found=len(clusters),
        memories_consolidated=total_consolidated,
        memories_archived=total_archived,
        new_summaries_created=new_summaries,
    )


# ---------------------------------------------------------------------------
# Contradiction reconciliation (A3) — human-confirmed supersede + archive
# ---------------------------------------------------------------------------


@dataclass
class ReconciliationCandidate:
    """A high-similarity, diverging pair proposed for human-confirmed supersede.

    ``newer_id`` (the more recently updated memory) would supersede and archive
    ``older_id``. Detection is read-only; nothing is applied without an explicit
    confirm via :func:`apply_reconciliation`.
    """

    newer_id: str
    older_id: str
    similarity: float


class ReconciliationError(ValueError):
    """Raised for an invalid reconciliation request.

    Unknown id, a pinned (``forever-keep``) memory, an already-archived older, or
    an inverted direction (``newer_id`` is not the more recent of the pair). A
    subclass of ``ValueError`` so the REST layer can map it to HTTP 400.
    """


def _decode_embedding(raw: object) -> list[float] | None:
    """Decode a stored embedding, tolerating vec0 bytes / JSON text / list form.

    Returns ``None`` on any malformed value (bad bytes length, non-JSON text) so
    a single corrupt row can't crash a whole reconciliation scan.
    """
    try:
        if isinstance(raw, bytes):
            return np.frombuffer(raw, dtype=np.float32).tolist()
        if isinstance(raw, str):
            return json.loads(raw)
        return list(raw)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _read_embeddings(
    conn: sqlite3.Connection, ids: list[str]
) -> dict[str, list[float]]:
    """Batch-read embeddings for *ids* in a single query (avoids N point SELECTs)."""
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, embedding FROM memories_vec WHERE id IN ({placeholders})",  # noqa: S608
        ids,
    ).fetchall()
    out: dict[str, list[float]] = {}
    for row in rows:
        mid = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        raw = row["embedding"] if isinstance(row, sqlite3.Row) else row[1]
        vec = _decode_embedding(raw)
        if vec is not None:
            out[mid] = vec
    return out


def _parse_ts(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware/naive datetime, or ``None``.

    Comparing parsed datetimes (not raw strings) makes ordering correct across
    heterogeneous ISO formats (``Z`` vs ``+00:00``, differing offsets/precision).
    """
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def find_reconciliation_candidates(
    conn: sqlite3.Connection,
    similarity_threshold: float = 0.85,
    limit: int = 100,
) -> list[ReconciliationCandidate]:
    """Detect near-duplicate / contradictory pairs among LIVE memories.

    Read-only. Reuses the same cosine machinery as :func:`find_clusters`, at the
    PAIR level (a supersede is pairwise). A pair ``(a, b)`` is a candidate when:

    - cosine similarity >= *similarity_threshold* (default 0.85 — the system's
      own near-duplicate bar, i.e. dedup's ``0.15`` cosine distance; the 0.75
      clustering threshold is for non-destructive 3+ merges and is too loose for
      a destructive pairwise supersede),
    - they have DISTINCT ``created_at`` (a clear newer to keep / older to
      supersede). **Direction uses ``created_at``, which is immutable — NOT
      ``updated_at``, which the aging cycle rewrites and would invert stale vs
      current** (a stale memory not accessed today gets a fresh ``updated_at``),
    - neither is pinned (``forever-keep``), and
    - the newer does not already supersede something (single-valued link).

    Candidates are sorted by descending similarity and capped at *limit*. This
    NEVER mutates — application is a separate, human-confirmed step
    (:func:`apply_reconciliation`); nothing is auto-superseded.
    """
    memories = [m for m in get_non_archived_memories(conn) if not _is_pinned(m)]
    if len(memories) < 2:
        return []

    embeddings = _read_embeddings(conn, [m.id for m in memories])
    if len(embeddings) < 2:
        return []

    # Guard a mixed-model corpus: keep only the dominant embedding dimension so
    # ragged vectors can't crash np.array (reconciliation scans ALL live tiers,
    # unlike run_consolidation's cold-only set).
    target_dim = Counter(len(v) for v in embeddings.values()).most_common(1)[0][0]
    embeddings = {k: v for k, v in embeddings.items() if len(v) == target_dim}

    embeddable = [m for m in memories if m.id in embeddings]
    if len(embeddable) < 2:
        return []

    ids: list[str] = [m.id for m in embeddable]
    by_id: dict[str, MemoryRecord] = {m.id: m for m in embeddable}
    vectors: np.ndarray = np.array([embeddings[i] for i in ids], dtype=np.float32)
    sim: np.ndarray = _cosine_similarity_matrix(vectors)

    # Vectorized: only the above-threshold upper-triangle pairs, so the Python
    # loop runs over the (few) real candidates rather than every N^2 pair.
    candidates: list[ReconciliationCandidate] = []
    for i, j in np.argwhere(np.triu(sim >= similarity_threshold, k=1)):
        i, j = int(i), int(j)
        a, b = by_id[ids[i]], by_id[ids[j]]
        ca, cb = _parse_ts(a.created_at), _parse_ts(b.created_at)
        if ca is None or cb is None or ca == cb:
            continue  # no clear / parseable newer
        newer, older = (a, b) if ca > cb else (b, a)
        if newer.supersedes is not None or older.supersedes == newer.id:
            continue  # already reconciled / newer already supersedes something
        candidates.append(
            ReconciliationCandidate(
                newer_id=newer.id,
                older_id=older.id,
                similarity=round(float(sim[i, j]), 4),
            )
        )

    candidates.sort(key=lambda c: c.similarity, reverse=True)
    return candidates[:limit]


def apply_reconciliation(
    conn: sqlite3.Connection,
    newer_id: str,
    older_id: str,
) -> dict:
    """Apply a HUMAN-CONFIRMED reconciliation: newer supersedes older, older archived.

    The only mutating half of A3; call it only after an explicit confirm. It
    NEVER hard-deletes — the older moves to the ``archived`` tier and
    ``newer.supersedes`` records the lineage (resolvable via ``memory_why``).

    Raises :class:`ReconciliationError` (HTTP 400) on: a self-pair; an unknown
    id; a pinned memory; an archived ``newer`` (would keep an invisible memory)
    or already-archived ``older``; a ``newer`` that already supersedes something
    (would clobber lineage); or a non-strict direction — the kept memory must be
    strictly newer by **created_at** (immutable), so we never archive the newer.
    """
    if newer_id == older_id:
        raise ReconciliationError("newer_id and older_id must differ")
    newer = get_memory(conn, newer_id)
    if newer is None:
        raise ReconciliationError(f"memory not found: {newer_id}")
    older = get_memory(conn, older_id)
    if older is None:
        raise ReconciliationError(f"memory not found: {older_id}")
    if _is_pinned(newer) or _is_pinned(older):
        raise ReconciliationError("cannot reconcile a forever-keep (pinned) memory")
    if newer.tier == "archived":
        raise ReconciliationError(f"{newer_id} (the kept memory) is archived")
    if older.tier == "archived":
        raise ReconciliationError(f"{older_id} is already archived")
    if newer.supersedes is not None:
        raise ReconciliationError(
            f"{newer_id} already supersedes {newer.supersedes}; would overwrite lineage"
        )
    cn, co = _parse_ts(newer.created_at), _parse_ts(older.created_at)
    if cn is None or co is None:
        raise ReconciliationError("unparseable created_at on one of the memories")
    if cn <= co:
        raise ReconciliationError(
            "direction invalid: newer_id must be strictly newer (by created_at)"
        )

    update_memory(conn, newer_id, supersedes=older_id)
    update_memory(conn, older_id, tier="archived")
    return {
        "reconciled": True,
        "superseded": older_id,
        "superseded_by": newer_id,
    }
