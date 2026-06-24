"""Typed query functions for the claude-memory database layer."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class MemoryRecord:
    """In-memory representation of a single row in the *memories* table."""

    id: str
    content: str
    summary: str | None
    type: str
    tags: list[str]
    created_at: str
    updated_at: str
    last_accessed: str
    access_count: int
    importance: float
    tier: str
    project_dir: str | None
    source_session: str | None
    supersedes: str | None
    consolidated_from: list[str]
    metadata: dict = field(default_factory=dict)


# ── Helpers ──────────────────────────────────────────────────────────────


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    """Convert a :class:`sqlite3.Row` to a :class:`MemoryRecord`."""
    return MemoryRecord(
        id=row["id"],
        content=row["content"],
        summary=row["summary"],
        type=row["type"],
        tags=json.loads(row["tags"]) if row["tags"] else [],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_accessed=row["last_accessed"],
        access_count=row["access_count"],
        importance=row["importance"],
        tier=row["tier"],
        project_dir=row["project_dir"],
        source_session=row["source_session"],
        supersedes=row["supersedes"],
        consolidated_from=(
            json.loads(row["consolidated_from"]) if row["consolidated_from"] else []
        ),
        metadata=json.loads(row["metadata"]) if row["metadata"] else {},
    )


def _now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


# ── CRUD ─────────────────────────────────────────────────────────────────


def insert_memory(
    conn: sqlite3.Connection,
    record: MemoryRecord,
    embedding: list[float],
) -> str:
    """Insert a memory into both *memories* and *memories_vec*.

    Returns the memory ``id`` for convenience.
    """
    conn.execute(
        """\
        INSERT INTO memories (
            id, content, summary, type, tags,
            created_at, updated_at, last_accessed,
            access_count, importance, tier,
            project_dir, source_session, supersedes,
            consolidated_from, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.id,
            record.content,
            record.summary,
            record.type,
            json.dumps(record.tags),
            record.created_at,
            record.updated_at,
            record.last_accessed,
            record.access_count,
            record.importance,
            record.tier,
            record.project_dir,
            record.source_session,
            record.supersedes,
            json.dumps(record.consolidated_from),
            json.dumps(record.metadata),
        ),
    )

    conn.execute(
        "INSERT INTO memories_vec (id, embedding) VALUES (?, ?)",
        (record.id, json.dumps(embedding)),
    )

    return record.id


def get_memory(conn: sqlite3.Connection, id: str) -> MemoryRecord | None:
    """Fetch a single memory by primary key, or ``None`` if not found."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM memories WHERE id = ?", (id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_record(row)


def update_memory(conn: sqlite3.Connection, id: str, **fields: object) -> None:
    """Partial update — set only the provided columns.

    JSON-serialisable fields (``tags``, ``consolidated_from``, ``metadata``)
    are automatically dumped to JSON strings.  The ``updated_at`` timestamp
    is always refreshed.

    Raises :class:`ValueError` if no fields are supplied.
    """
    if not fields:
        raise ValueError("update_memory requires at least one field to update")

    json_fields: set[str] = {"tags", "consolidated_from", "metadata"}
    processed: dict[str, object] = {}
    for key, value in fields.items():
        if key in json_fields:
            processed[key] = json.dumps(value)
        else:
            processed[key] = value

    # Always bump updated_at.
    processed["updated_at"] = _now_iso()

    set_clause: str = ", ".join(f"{col} = ?" for col in processed)
    values: list[object] = list(processed.values())
    values.append(id)

    conn.execute(
        f"UPDATE memories SET {set_clause} WHERE id = ?",  # noqa: S608
        values,
    )


def delete_memory(conn: sqlite3.Connection, id: str) -> None:
    """Delete a memory from both *memories* and *memories_vec*."""
    conn.execute("DELETE FROM memories WHERE id = ?", (id,))
    conn.execute("DELETE FROM memories_vec WHERE id = ?", (id,))


# ── Search ───────────────────────────────────────────────────────────────

# Characters that have special meaning in FTS5 query syntax.
_FTS5_SPECIAL_CHARS = re.compile(r'["\(\)\*\+\-\:\^\{\}\?]')


def _sanitize_fts_query(query: str) -> str:
    """Escape a user-provided string for safe use in an FTS5 MATCH clause.

    Strips characters with special FTS5 meaning and wraps each remaining
    token in double quotes so it is treated as a literal term.  Returns an
    empty string if no usable tokens remain.
    """
    # Remove special characters.
    cleaned: str = _FTS5_SPECIAL_CHARS.sub(" ", query)
    # Split into tokens, wrap each in quotes.
    tokens: list[str] = [f'"{t}"' for t in cleaned.split() if t]
    return " ".join(tokens)


def _l2_to_cosine_distance(l2_dist: float) -> float:
    """Convert L2 (Euclidean) distance to cosine distance for normalised vectors.

    For unit-length vectors: L2² = 2·(1 − cos_sim), so cos_dist = L2²/2.
    Result is clamped to [0.0, 2.0].
    """
    return min(max((l2_dist * l2_dist) / 2.0, 0.0), 2.0)


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    project_dir: str | None = None,
    top_k: int = 50,
) -> list[tuple[str, float]]:
    """Full-text search via FTS5.

    Returns a list of ``(memory_id, rank)`` tuples ordered by relevance
    (lower rank = better match in FTS5's BM25 scoring).  When *project_dir*
    is provided, results are filtered to memories scoped to that directory
    or global (no project_dir).
    """
    safe_query: str = _sanitize_fts_query(query)
    if not safe_query:
        return []

    if project_dir is not None:
        rows = conn.execute(
            """\
            SELECT m.id, fts.rank
            FROM memories_fts AS fts
            JOIN memories AS m ON m.rowid = fts.rowid
            WHERE memories_fts MATCH ?
              AND (m.project_dir IS NULL OR m.project_dir = ?)
            ORDER BY fts.rank
            LIMIT ?
            """,
            (safe_query, project_dir, top_k),
        ).fetchall()
    else:
        rows = conn.execute(
            """\
            SELECT m.id, fts.rank
            FROM memories_fts AS fts
            JOIN memories AS m ON m.rowid = fts.rowid
            WHERE memories_fts MATCH ?
            ORDER BY fts.rank
            LIMIT ?
            """,
            (safe_query, top_k),
        ).fetchall()
    return [(row["id"], row["rank"]) for row in rows]


def search_vec(
    conn: sqlite3.Connection,
    embedding: list[float],
    top_k: int = 50,
) -> list[tuple[str, float]]:
    """Vector similarity search via sqlite-vec.

    Returns a list of ``(memory_id, cosine_distance)`` tuples ordered by
    ascending distance (lower = more similar).

    sqlite-vec's ``vec0`` virtual table returns **L2 (Euclidean) distance**.
    Since all embeddings are L2-normalised, we convert to cosine distance
    via ``cosine_dist = L2² / 2`` so that downstream consumers get a
    consistent [0, 2] metric.
    """
    rows = conn.execute(
        """\
        SELECT id, distance
        FROM memories_vec
        WHERE embedding MATCH ?
        ORDER BY distance
        LIMIT ?
        """,
        (json.dumps(embedding), top_k),
    ).fetchall()
    return [
        (row["id"], _l2_to_cosine_distance(row["distance"]))
        for row in rows
    ]


# ── Retrieval helpers ────────────────────────────────────────────────────


def get_always_load(
    conn: sqlite3.Connection,
    project_dir: str | None,
    importance_threshold: float = 7.0,
) -> list[str]:
    """Return IDs of high-importance memories that should always be loaded.

    Selects memories whose importance meets the threshold *and* that either
    have no ``project_dir`` (global) or match the given *project_dir*.
    """
    rows = conn.execute(
        """\
        SELECT id FROM memories
        WHERE importance >= ?
          AND tier != 'archived'
          AND (project_dir IS NULL OR project_dir = ?)
        ORDER BY importance DESC
        """,
        (importance_threshold, project_dir),
    ).fetchall()
    return [row["id"] for row in rows]


def update_access(conn: sqlite3.Connection, ids: str | list[str]) -> None:
    """Bump ``last_accessed`` and ``access_count`` for the given ID(s).

    Accepts a single ID string or a list of IDs.
    """
    if isinstance(ids, str):
        ids = [ids]
    if not ids:
        return

    now: str = _now_iso()
    placeholders: str = ", ".join("?" for _ in ids)
    conn.execute(
        f"""\
        UPDATE memories
        SET last_accessed = ?,
            access_count  = access_count + 1
        WHERE id IN ({placeholders})
        """,  # noqa: S608
        [now, *ids],
    )


def get_memories_by_tier(
    conn: sqlite3.Connection,
    tier: str,
) -> list[MemoryRecord]:
    """Return all memories belonging to the given tier."""
    rows = conn.execute(
        "SELECT * FROM memories WHERE tier = ? ORDER BY importance DESC",
        (tier,),
    ).fetchall()
    return [_row_to_record(row) for row in rows]


# ── Analytics / maintenance ──────────────────────────────────────────────


def get_stats(conn: sqlite3.Connection) -> dict:
    """Return aggregate counts grouped by type and tier.

    Returns a dict with keys ``by_type``, ``by_tier``, and ``total``.
    """
    type_rows = conn.execute(
        "SELECT type, COUNT(*) AS cnt FROM memories GROUP BY type"
    ).fetchall()
    tier_rows = conn.execute(
        "SELECT tier, COUNT(*) AS cnt FROM memories GROUP BY tier"
    ).fetchall()
    total_row = conn.execute("SELECT COUNT(*) AS cnt FROM memories").fetchone()

    return {
        "by_type": {row["type"]: row["cnt"] for row in type_rows},
        "by_tier": {row["tier"]: row["cnt"] for row in tier_rows},
        "total": total_row["cnt"] if total_row else 0,
    }


def bulk_update_importance(
    conn: sqlite3.Connection,
    decay_rate: float,
) -> int:
    """Apply importance decay to all non-archived memories not accessed today.

    Each qualifying memory's importance is multiplied by *decay_rate* (e.g.
    0.995), clamped to a minimum of 0.1.

    Memories tagged ``forever-keep`` are exempted — their importance is
    preserved across aging cycles. This is the pinning mechanism for
    knowledge the user never wants to lose (e.g. core preferences, critical
    gotchas).

    Returns the number of rows affected.
    """
    today: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cursor: sqlite3.Cursor = conn.execute(
        """\
        UPDATE memories
        SET importance = MAX(0.1, importance * ?),
            updated_at = ?
        WHERE tier != 'archived'
          AND date(last_accessed) < ?
          AND (tags IS NULL OR tags NOT LIKE '%"forever-keep"%')
        """,
        (decay_rate, _now_iso(), today),
    )
    return cursor.rowcount


def update_tiers(
    conn: sqlite3.Connection,
    hot_access_threshold: int,
    warm_days: int,
    cold_days: int,
    cold_importance_threshold: float,
) -> tuple[int, int, int]:
    """Re-evaluate tier placement for every non-archived memory.

    Memories tagged ``forever-keep`` are pinned: tier-update SQL skips them,
    so they neither promote nor demote. Combine with the importance-decay
    exemption in :func:`bulk_update_importance` and these memories are
    effectively immortal.

    Returns ``(promoted_to_hot, demoted_to_warm, demoted_to_cold)``.
    """
    now: str = _now_iso()
    # Shared exclusion: applied to every UPDATE in this function so
    # forever-keep memories never move tier.
    _pinned_exclusion: str = "(tags IS NULL OR tags NOT LIKE '%\"forever-keep\"%')"

    # Promote to hot: frequently accessed in the recent window.
    cur = conn.execute(
        f"""\
        UPDATE memories
        SET tier = 'hot', updated_at = ?
        WHERE tier != 'archived'
          AND access_count >= ?
          AND julianday('now') - julianday(last_accessed) <= ?
          AND {_pinned_exclusion}
        """,
        (now, hot_access_threshold, warm_days),
    )
    promoted_to_hot: int = cur.rowcount

    # Demote from hot to warm: not frequently accessed.
    cur = conn.execute(
        f"""\
        UPDATE memories
        SET tier = 'warm', updated_at = ?
        WHERE tier = 'hot'
          AND (
              access_count < ?
              OR julianday('now') - julianday(last_accessed) > ?
          )
          AND {_pinned_exclusion}
        """,
        (now, hot_access_threshold, warm_days),
    )
    demoted_to_warm: int = cur.rowcount

    # Demote from warm to cold: stale and low importance.
    cur = conn.execute(
        f"""\
        UPDATE memories
        SET tier = 'cold', updated_at = ?
        WHERE tier = 'warm'
          AND julianday('now') - julianday(last_accessed) > ?
          AND importance <= ?
          AND {_pinned_exclusion}
        """,
        (now, warm_days, cold_importance_threshold),
    )
    demoted_to_cold: int = cur.rowcount

    # Promote cold back to warm if importance has risen.
    conn.execute(
        f"""\
        UPDATE memories
        SET tier = 'warm', updated_at = ?
        WHERE tier = 'cold'
          AND importance > ?
          AND {_pinned_exclusion}
        """,
        (now, cold_importance_threshold),
    )

    # Demote cold to archived if very stale.
    conn.execute(
        f"""\
        UPDATE memories
        SET tier = 'archived', updated_at = ?
        WHERE tier = 'cold'
          AND julianday('now') - julianday(last_accessed) > ?
          AND importance <= ?
          AND {_pinned_exclusion}
        """,
        (now, cold_days, cold_importance_threshold),
    )

    return promoted_to_hot, demoted_to_warm, demoted_to_cold
