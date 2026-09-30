"""Typed query functions for the claude-memory database layer."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from claude_memory.scope import (
    canonicalize_project_dir,
    is_read_scope_compatible,
    is_recall_scope_compatible,
)


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


def _register_read_scope_function(conn: sqlite3.Connection) -> None:
    """Register canonical project-scope matching for scoped SQL queries."""
    conn.create_function(
        "j5_read_scope_compatible",
        2,
        lambda record_scope, requested_scope: int(
            is_read_scope_compatible(record_scope, requested_scope)
        ),
        deterministic=True,
    )


def _register_recall_scope_function(conn: sqlite3.Connection) -> None:
    """Register global-or-canonical-project matching for recall queries."""
    conn.create_function(
        "j5_recall_scope_compatible",
        2,
        lambda record_scope, requested_scope: int(
            is_recall_scope_compatible(record_scope, requested_scope)
        ),
        deterministic=True,
    )


def _utc_microseconds(value: object) -> int | None:
    """Exact UTC microseconds since the epoch for an ISO-8601 string, else None."""
    if not isinstance(value, str):
        return None
    try:
        dt: datetime = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Direct aware subtraction: converting to UTC first would overflow for
        # parseable values at the calendar edge and abort the whole lookup.
        return (dt - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(
            microseconds=1
        )
    except (ValueError, OverflowError):
        return None


def _register_utc_microseconds_function(conn: sqlite3.Connection) -> None:
    """Register ``j5_utc_microseconds(text)`` for exact instant ordering."""
    conn.create_function(
        "j5_utc_microseconds", 1, _utc_microseconds, deterministic=True,
    )


def _register_recall_fts_scope_function(conn: sqlite3.Connection) -> None:
    """Register the recall predicate under an FTS-specific SQLite name."""
    conn.create_function(
        "j5_recall_fts_scope_compatible",
        2,
        lambda record_scope, requested_scope: int(
            is_recall_scope_compatible(record_scope, requested_scope)
        ),
        deterministic=True,
    )


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
    top_k: int | None = 50,
    recall_scope: bool = False,
) -> list[tuple[str, float]]:
    """Full-text search via FTS5.

    Returns a list of ``(memory_id, rank)`` tuples ordered by relevance
    (lower rank = better match in FTS5's BM25 scoring).  When *project_dir*
    is provided, results are filtered to memories scoped to that directory
    or global (no project_dir). When *recall_scope* is true, the recall-specific
    global-or-canonical-project predicate is applied before the result limit.

    *top_k* of ``None`` omits the ``LIMIT`` clause entirely (unlike
    :func:`search_vec`, FTS5 has no engine-level requirement for one — a
    caller doing its own downstream filtering, e.g. a strict tags[] AND
    filter, can ask for the whole matching set rather than risk a bound
    truncating away the row that filter needs).
    """
    safe_query: str = _sanitize_fts_query(query)
    if not safe_query:
        return []

    limit_clause: str = "\n            LIMIT ?" if top_k is not None else ""
    limit_params: tuple[int, ...] = (top_k,) if top_k is not None else ()

    if recall_scope:
        _register_recall_fts_scope_function(conn)
        rows = conn.execute(
            f"""\
            SELECT m.id, fts.rank
            FROM memories_fts AS fts
            JOIN memories AS m ON m.rowid = fts.rowid
            WHERE memories_fts MATCH ?
              AND j5_recall_fts_scope_compatible(m.project_dir, ?) = 1
            ORDER BY fts.rank{limit_clause}
            """,  # noqa: S608 — limit_clause is one of two fixed literals above
            (safe_query, project_dir, *limit_params),
        ).fetchall()
    elif project_dir is not None:
        _register_read_scope_function(conn)
        rows = conn.execute(
            f"""\
            SELECT m.id, fts.rank
            FROM memories_fts AS fts
            JOIN memories AS m ON m.rowid = fts.rowid
            WHERE memories_fts MATCH ?
              AND j5_read_scope_compatible(m.project_dir, ?) = 1
            ORDER BY fts.rank{limit_clause}
            """,  # noqa: S608
            (safe_query, project_dir, *limit_params),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""\
            SELECT m.id, fts.rank
            FROM memories_fts AS fts
            JOIN memories AS m ON m.rowid = fts.rowid
            WHERE memories_fts MATCH ?
            ORDER BY fts.rank{limit_clause}
            """,  # noqa: S608
            (safe_query, *limit_params),
        ).fetchall()
    return [(row["id"], row["rank"]) for row in rows]


def _tags_exact_predicate(tags: list[str]) -> tuple[str, list[str]]:
    """Build an AND-chained exact-membership predicate for every tag in *tags*.

    One ``EXISTS (SELECT 1 FROM json_each(tags) WHERE value = ?)`` per tag.
    Tags are stored via ``json.dumps`` (``ensure_ascii=True``), so a ``LIKE``
    over the raw text misses non-ASCII / quote / backslash tags (false
    negatives) and ``LIKE``'s ``%``/``_`` wildcards and case-insensitivity
    admit decoys that fill the KNN LIMIT before the strict filter runs
    (bound-before-filter). ``json_each`` compares decoded, case-sensitive
    values, so this equals the strict subset filter in ``retrieval/search.py``.
    """
    clauses: list[str] = []
    params: list[str] = []
    for tag in tags:
        clauses.append("EXISTS (SELECT 1 FROM json_each(tags) WHERE value = ?)")
        params.append(tag)
    return " AND ".join(clauses), params


def search_vec(
    conn: sqlite3.Connection,
    embedding: list[float],
    top_k: int = 50,
    project_dir: str | None = None,
    recall_scope: bool = False,
    required_tags: list[str] | None = None,
) -> list[tuple[str, float]]:
    """Vector similarity search via sqlite-vec.

    Returns a list of ``(memory_id, cosine_distance)`` tuples ordered by
    ascending distance (lower = more similar).

    sqlite-vec's ``vec0`` virtual table returns **L2 (Euclidean) distance**.
    Since all embeddings are L2-normalised, we convert to cosine distance
    via ``cosine_dist = L2² / 2`` so that downstream consumers get a
    consistent [0, 2] metric.

    Scope (*project_dir* / *recall_scope*) and *required_tags* (exact,
    case-sensitive membership via ``json_each``, identical to the strict
    filter in ``retrieval/search.py``) are both applied as an ``id IN (subquery)`` pre-filter against *memories* — NOT a
    ``JOIN``. vec0's KNN operator requires its ``LIMIT`` (or an explicit
    ``k = ?``) to be visible to its own query planner; introducing a
    ``JOIN`` between ``memories_vec`` and ``memories`` hides that from
    vec0's ``xBestIndex`` and it refuses the query outright — confirmed
    directly against the real extension (sqlite-vec 0.1.9):
    ``OperationalError: A LIMIT or 'k = ?' constraint is required on vec0
    knn queries``. ``id IN (SELECT id FROM memories WHERE ...)`` is, by
    contrast, a constraint vec0 recognises directly against its own declared
    ``id`` primary-key column (verified empirically: with an adversarial
    corpus where every ineligible row is closer than every eligible one,
    the eligible set still returns in full) — so it narrows the KNN
    candidate set *before* nearest neighbours are computed, a genuine
    pre-filter rather than a bound-then-filter. *top_k* only ever trims
    within the already-eligible set, so it can stay at the caller's normal
    pool size regardless of whether scope or tags apply — unlike
    :func:`search_fts` / ``get_always_load``, there is no unbounded mode
    here: vec0 rejects a KNN query with no bound at all, so when neither
    scope nor tags apply, *top_k* is still the only constraint (matching
    the original, always-worked shape exactly).
    """
    predicates: list[str] = []
    params: list[object] = []

    if recall_scope:
        _register_recall_scope_function(conn)
        predicates.append("j5_recall_scope_compatible(project_dir, ?) = 1")
        params.append(project_dir)
    elif project_dir is not None:
        _register_read_scope_function(conn)
        predicates.append("j5_read_scope_compatible(project_dir, ?) = 1")
        params.append(project_dir)

    if required_tags:
        tag_sql, tag_params = _tags_exact_predicate(required_tags)
        predicates.append(tag_sql)
        params.extend(tag_params)

    query_embedding: str = json.dumps(embedding)
    if predicates:
        where_clause: str = " AND ".join(predicates)
        rows = conn.execute(
            f"""\
            SELECT id, distance
            FROM memories_vec
            WHERE embedding MATCH ?
              AND id IN (SELECT id FROM memories WHERE {where_clause})
            ORDER BY distance
            LIMIT ?
            """,  # noqa: S608 — where_clause is only ever built from the two
            # fixed, parametrised fragments above; never from caller-supplied
            # SQL text.
            (query_embedding, *params, top_k),
        ).fetchall()
    else:
        rows = conn.execute(
            """\
            SELECT id, distance
            FROM memories_vec
            WHERE embedding MATCH ?
            ORDER BY distance
            LIMIT ?
            """,
            (query_embedding, top_k),
        ).fetchall()
    return [
        (row["id"], _l2_to_cosine_distance(row["distance"]))
        for row in rows
    ]


# ── Retrieval helpers ────────────────────────────────────────────────────


# Splitting the always-load set needs a SQL-side answer to "does this memory
# claim a project of its own?". Asking the already-registered scope predicate
# whether the record is compatible with the *global* scope answers exactly
# that, and reuses the Python canonicalisation rules (NULL and blank alike are
# unclaimed) rather than restating them in SQL where they could drift.
_ALWAYS_LOAD_SCOPE_PREDICATES: dict[str, str] = {
    "any": "",
    "project": " AND j5_recall_scope_compatible(project_dir, NULL) = 0",
    "global": " AND j5_recall_scope_compatible(project_dir, NULL) = 1",
}


def _always_load_page(
    conn: sqlite3.Connection,
    project_dir: str | None,
    importance_threshold: float,
    limit: int | None,
    scope: str = "any",
) -> list[str]:
    """Return one ordered page of always-load ids, optionally scope-restricted.

    Ordering is importance first, then creation order, then id, so that a
    ``limit`` truncates the *least* useful end of the set rather than an
    arbitrary one — ``ORDER BY importance DESC`` alone leaves thousands of
    ties on a mature corpus. The tiebreak deliberately avoids
    ``last_accessed``: every retrieval bumps it via ``update_access``, which
    would couple this ordering to the very read-driven feedback loop the
    bound exists to break — a memory that happened to be retrieved once
    would leapfrog an equally-important, never-read memory on every
    subsequent call, regardless of relative merit. ``created_at`` does not
    change after insert, so it breaks importance ties without that feedback.
    ``id`` is the final tiebreak for the (rare) case two rows share both —
    ids are ULIDs, which sort monotonically with creation order, so this
    stays a stable, fully deterministic ordering.
    """
    sql: str = f"""\
        SELECT id FROM memories
        WHERE importance >= ?
          AND tier != 'archived'
          AND j5_recall_scope_compatible(project_dir, ?) = 1
          {_ALWAYS_LOAD_SCOPE_PREDICATES[scope]}
        ORDER BY importance DESC, created_at DESC, id DESC
        """  # noqa: S608 — interpolated fragment is a lookup in a fixed map
    # This ORDER BY only owns which ids make it into the bounded page (SET
    # stability) — a fixed, feedback-immune tiebreak so `limit` truncates the
    # same members every time. It does NOT own the RANK those members end up
    # at in a final recall/search result: recency is a first-class scorer
    # term there by design (claude_memory.retrieval.scorer), so a member's
    # position in the scored output legitimately moves as it is re-accessed.
    # That rank oscillation is scorer-domain, not this page's contract.
    params: list[object] = [importance_threshold, project_dir]
    if limit is not None:
        sql += "        LIMIT ?\n"
        params.append(limit)
    return [row["id"] for row in conn.execute(sql, params).fetchall()]


def get_always_load(
    conn: sqlite3.Connection,
    project_dir: str | None,
    importance_threshold: float = 7.0,
    limit: int | None = None,
) -> list[str]:
    """Return IDs of high-importance memories that should always be loaded.

    Selects memories whose importance meets the threshold *and* that either
    have no ``project_dir`` (global) or match the given *project_dir*.

    Parameters
    ----------
    limit:
        Maximum number of ids to return. ``None`` (the default) returns the
        whole qualifying set, which is what diagnostics and direct callers
        want. Retrieval callers should always pass a bound: on a mature corpus
        the importance threshold alone qualifies the majority of the database,
        at which point "always load" stops being a priority set and simply
        floods the candidate pool (issue #29).

        When a bound is set and the caller named a project, it is split
        between that project's own memories and the global pool — half
        reserved for each, with either side free to claim capacity the other
        does not use. Without the reservation a project large enough to fill
        the bound on its own would evict every global preference, and a small
        project would see its own context evicted by globals.
    """
    _register_recall_scope_function(conn)

    if limit is None:
        return _always_load_page(conn, project_dir, importance_threshold, None)
    if limit <= 0:
        return []

    if canonicalize_project_dir(project_dir) is None:
        # Only global memories are recall-compatible with an unscoped caller,
        # so there is no second pool to reserve capacity for.
        return _always_load_page(conn, project_dir, importance_threshold, limit)

    scoped: list[str] = _always_load_page(
        conn, project_dir, importance_threshold, limit, scope="project",
    )
    global_ids: list[str] = _always_load_page(
        conn, project_dir, importance_threshold, limit, scope="global",
    )

    # Each pool is guaranteed its half and may grow into whatever the other
    # pool leaves unclaimed.
    reserved_for_project: int = limit // 2
    take_scoped: int = min(
        len(scoped), max(reserved_for_project, limit - len(global_ids)),
    )
    take_global: int = min(len(global_ids), limit - take_scoped)
    return [*scoped[:take_scoped], *global_ids[:take_global]]


_SESSION_STATE_TAGS: tuple[str, ...] = (
    "session-state", "precompact", "kind:session-state",
)


def get_latest_session_state(
    conn: sqlite3.Connection,
    project_dir: str | None,
    tags: tuple[str, ...] = _SESSION_STATE_TAGS,
) -> sqlite3.Row | None:
    """Return the newest non-archived session-state memory for *project_dir*.

    Deliberate choices:

    - Deterministic by authored time (exact UTC instant of ``created_at``,
      ``id`` as tiebreak), independent of ranking, importance, tier or access.
    - No importance floor.
    - No global fallback: scope is a canonical exact match to a *scoped* row,
      because a foreign resume block is worse than none.
    - The tag set is wide because rows tagged only ``kind:session-state`` exist.
    - Read-only: access stats feed the recency term this lookup exists to
      bypass, so nothing is updated and nothing is committed.

    Returns ``None`` for a blank *project_dir*, empty *tags*, or no match.
    """
    if project_dir is None or not project_dir.strip() or not tags:
        return None
    _register_recall_scope_function(conn)
    _register_utc_microseconds_function(conn)
    placeholders: str = ", ".join("?" for _ in tags)
    return conn.execute(
        f"""        SELECT id, content, type, tags, importance, created_at, project_dir
        FROM memories
        WHERE j5_recall_scope_compatible(project_dir, ?) = 1
          AND j5_recall_scope_compatible(project_dir, NULL) = 0
          AND tier != 'archived'
          AND EXISTS (
            SELECT 1 FROM json_each(memories.tags) WHERE value IN ({placeholders})
          )
        ORDER BY j5_utc_microseconds(created_at) DESC, id DESC
        LIMIT 1
        """,  # noqa: S608 — placeholders is only ever "?, ?, ..."; values are bound.
        (project_dir, *tags),
    ).fetchone()


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


def get_non_archived_memories(conn: sqlite3.Connection) -> list[MemoryRecord]:
    """Return every live (non-archived) memory.

    The scan set for reconciliation-candidate detection (A3): archived rows are
    already the pruned state, so they are never reconciliation targets.
    """
    rows = conn.execute(
        "SELECT * FROM memories WHERE tier != 'archived'"
    ).fetchall()
    return [_row_to_record(row) for row in rows]


# ── List / browse (dashboard, A1) ─────────────────────────────────────────


class ListQueryError(ValueError):
    """Raised for an invalid ``list_memories`` sort/order/filter argument.

    A subclass of :class:`ValueError` so callers may catch it *specifically* —
    distinct from a :class:`json.JSONDecodeError` (also a ``ValueError``) raised
    while decoding a corrupt row. The former is a bad request (HTTP 400); the
    latter is a server-data fault (HTTP 500) and must not be mislabelled.
    """


# Whitelists: user-supplied sort/order/filter are MAPPED to these fixed SQL
# fragments — never string-interpolated. Anything not present raises ValueError.
# tier/type values are always passed as bound parameters. This is the sole SQL
# injection barrier for the dashboard's browse endpoint.
_LIST_SORT_COLUMNS: dict[str, str] = {
    "access_count": "access_count",
    "importance": "importance",
    "created_at": "created_at",
    "last_accessed": "last_accessed",
}
_LIST_ORDERS: dict[str, str] = {"asc": "ASC", "desc": "DESC"}
_LIST_PRESET_FILTERS: dict[str, str] = {
    "never_retrieved": "access_count = 0",
    "unscoped": "project_dir IS NULL",
}


def list_memories(
    conn: sqlite3.Connection,
    *,
    sort: str = "created_at",
    order: str = "desc",
    filter: str | None = None,
    tier: str | None = None,
    type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_archived: bool = False,
) -> list[MemoryRecord]:
    """Paginated browse over the corpus for the dashboard (A1).

    ``sort`` / ``order`` / ``filter`` are **whitelist-mapped** to fixed SQL
    fragments (see the module-level maps) — never interpolated — so this path
    cannot be turned into SQL injection. ``tier`` / ``type`` are bound
    parameters; ``limit`` / ``offset`` are coerced to safe ints.

    - ``filter='never_retrieved'`` -> ``access_count = 0``
    - ``filter='unscoped'``        -> ``project_dir IS NULL``

    Archived rows are hidden unless an explicit ``tier`` is requested or
    ``include_archived`` is set — including under the ``never_retrieved`` /
    ``unscoped`` presets, so a forgotten (archived) row leaves those views and
    the chip row set equals the live ``get_stats`` counts they drill into
    (which also exclude archived). View archived via ``tier='archived'`` or
    ``include_archived=True``.

    Raises :class:`ListQueryError` on an unknown ``sort``, ``order``, or
    ``filter``.
    """
    sort_col: str | None = _LIST_SORT_COLUMNS.get(sort)
    if sort_col is None:
        raise ListQueryError(f"invalid sort column: {sort!r}")
    order_kw: str | None = _LIST_ORDERS.get(order.lower())
    if order_kw is None:
        raise ListQueryError(f"invalid order: {order!r}")

    clauses: list[str] = []
    params: list[object] = []

    if filter is not None:
        preset: str | None = _LIST_PRESET_FILTERS.get(filter)
        if preset is None:
            raise ListQueryError(f"invalid filter: {filter!r}")
        clauses.append(preset)

    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)

    if type is not None:
        clauses.append("type = ?")
        params.append(type)

    # Hide archived everywhere except an explicit tier request or opt-in, so the
    # live views (incl. the never_retrieved/unscoped chips) match the live stats.
    if tier is None and not include_archived:
        clauses.append("tier != 'archived'")

    where_sql: str = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    safe_limit: int = min(max(1, int(limit)), 1000)
    safe_offset: int = max(0, int(offset))
    params.extend([safe_limit, safe_offset])

    # sort_col/order_kw are whitelist constants (never user text); the secondary
    # `id` sort makes pagination deterministic across tied primary keys.
    sql: str = (
        f"SELECT * FROM memories {where_sql} "  # noqa: S608 - whitelisted identifiers only
        f"ORDER BY {sort_col} {order_kw}, id ASC LIMIT ? OFFSET ?"
    )
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_record(row) for row in rows]


# ── Analytics / maintenance ──────────────────────────────────────────────


def get_stats(conn: sqlite3.Connection, top_n: int = 15) -> dict:
    """Return aggregate counts plus the Tier A audit headline signals.

    Keys: ``by_type``, ``by_tier``, ``total`` (existing) and — added for A2
    observability — ``never_retrieved``, ``unscoped``, ``top_n_share``.

    The three audit signals are computed over the **live** corpus (``tier !=
    'archived'``) — archived is the pruned state, so counting it as prunable
    dead weight would mean forgetting a row never moves the number. ``by_type``,
    ``by_tier`` and ``total`` remain whole-corpus inventory (``by_tier`` reports
    the archived bucket).

    - ``never_retrieved``: live memories with ``access_count = 0`` (dead weight
      the audit prunes — 29% of the corpus at last measure).
    - ``unscoped``: live memories with ``project_dir IS NULL`` (global rows that
      leak across every project's recall).
    - ``top_n_share``: fraction of live retrievals (the sum of ``access_count``
      over live rows) concentrated in the ``top_n`` most-retrieved — the audit's
      concentration signal. ``0.0`` when nothing has been retrieved yet
      (guards against divide-by-zero on a cold corpus).

    Parameters
    ----------
    top_n:
        Size of the retrieval-concentration head (default 15 — the audit's
        headline cohort). Denominator is always the *total* live retrieval
        count, never the memory count.
    """
    type_rows = conn.execute(
        "SELECT type, COUNT(*) AS cnt FROM memories GROUP BY type"
    ).fetchall()
    tier_rows = conn.execute(
        "SELECT tier, COUNT(*) AS cnt FROM memories GROUP BY tier"
    ).fetchall()
    total_row = conn.execute("SELECT COUNT(*) AS cnt FROM memories").fetchone()

    never_retrieved_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM memories "
        "WHERE access_count = 0 AND tier != 'archived'"
    ).fetchone()
    unscoped_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM memories "
        "WHERE project_dir IS NULL AND tier != 'archived'"
    ).fetchone()

    total_accesses_row = conn.execute(
        "SELECT COALESCE(SUM(access_count), 0) AS s FROM memories "
        "WHERE tier != 'archived'"
    ).fetchone()
    total_accesses: int = total_accesses_row["s"] if total_accesses_row else 0

    top_n_row = conn.execute(
        """\
        SELECT COALESCE(SUM(access_count), 0) AS s FROM (
            SELECT access_count FROM memories
            WHERE tier != 'archived'
            ORDER BY access_count DESC
            LIMIT ?
        )
        """,
        (top_n,),
    ).fetchone()
    top_n_accesses: int = top_n_row["s"] if top_n_row else 0

    top_n_share: float = (
        round(top_n_accesses / total_accesses, 4) if total_accesses > 0 else 0.0
    )

    return {
        "by_type": {row["type"]: row["cnt"] for row in type_rows},
        "by_tier": {row["tier"]: row["cnt"] for row in tier_rows},
        "total": total_row["cnt"] if total_row else 0,
        "never_retrieved": never_retrieved_row["cnt"] if never_retrieved_row else 0,
        "unscoped": unscoped_row["cnt"] if unscoped_row else 0,
        "top_n_share": top_n_share,
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
