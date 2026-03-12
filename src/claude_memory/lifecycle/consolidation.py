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
    cold_memories: list[MemoryRecord] = get_memories_by_tier(conn, tier="cold")

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
        consolidated_record = MemoryRecord(
            id=new_id,
            content=summary_text,
            summary=None,
            type=majority_type,
            tags=json.dumps(merged_tags),
            created_at=now,
            updated_at=now,
            last_accessed=now,
            access_count=0,
            importance=max_importance,
            tier="warm",
            project_dir=cluster_memories[0].project_dir,
            source_session=None,
            supersedes=None,
            consolidated_from=json.dumps(cluster_ids),
            metadata=json.dumps({}),
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
