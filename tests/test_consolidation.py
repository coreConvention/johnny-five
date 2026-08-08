"""Regression tests for the consolidation write path (issue #11).

``run_consolidation`` must write consolidated rows whose list/dict columns
(``tags``, ``consolidated_from``, ``metadata``) round-trip as native Python
types — not as double-JSON-encoded strings.

The bug: ``consolidation.py`` pre-serialized these fields with ``json.dumps()``
before handing them to ``insert_memory``, which ``json.dumps()``-ed them a
*second* time. ``_row_to_record`` decodes once, so a consolidation-written row
came back with ``tags``/``consolidated_from`` as ``str`` and ``metadata`` as a
``str`` instead of ``list``/``list``/``dict``.

These tests exercise the real ``run_consolidation`` code path (not a simulated
double-encode), so re-introducing the ``json.dumps`` wrapper in the record
construction fails them.
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np

from claude_memory.db.queries import (
    MemoryRecord,
    get_memories_by_tier,
    insert_memory,
)
from claude_memory.lifecycle.consolidation import run_consolidation


def _cold_record(id: str, tags: list[str]) -> MemoryRecord:
    """A cold-tier record eligible for consolidation clustering."""
    ts = "2026-01-01T00:00:00+00:00"
    return MemoryRecord(
        id=id,
        content=f"cold memory {id} about tooling and workflow decisions.",
        summary=None,
        type="lesson",
        tags=tags,
        created_at=ts,
        updated_at=ts,
        last_accessed=ts,
        access_count=0,
        importance=5.0,
        tier="cold",
        project_dir=None,
        source_session=None,
        supersedes=None,
        consolidated_from=[],
        metadata={},
    )


def _seed_cold_cluster(
    conn: sqlite3.Connection, ids: list[str], vec: list[float]
) -> None:
    """Insert cold memories with *identical* embeddings so they cluster.

    The test ``memories_vec`` is a plain TEXT table, but ``run_consolidation``
    reads embeddings back expecting float32 **bytes** (``np.frombuffer``) — the
    format sqlite-vec's ``vec0`` virtual table returns in production. So after
    ``insert_memory`` writes its JSON-text vec row, overwrite it with a float32
    blob so clustering sees real vectors rather than a list of characters.
    """
    blob = np.array(vec, dtype=np.float32).tobytes()
    for i, mid in enumerate(ids):
        insert_memory(conn, _cold_record(mid, tags=[f"tag-{i}", "shared"]), vec)
        conn.execute(
            "UPDATE memories_vec SET embedding = ? WHERE id = ?", (blob, mid)
        )


def test_run_consolidation_roundtrips_native_types(
    db_conn: sqlite3.Connection, mock_encoder
) -> None:
    ids = ["cold-a", "cold-b", "cold-c"]
    _seed_cold_cluster(db_conn, ids, vec=[0.1] * 8)

    report = run_consolidation(db_conn, mock_encoder, min_cluster_size=3)

    # A cluster of three must have produced exactly one consolidated summary.
    assert report.new_summaries_created == 1
    assert report.memories_consolidated == 3

    warm = get_memories_by_tier(db_conn, "warm")
    assert len(warm) == 1
    consolidated = warm[0]

    # Core regression: native types, not double-encoded strings.
    assert isinstance(consolidated.tags, list), (
        f"tags should be list, got {type(consolidated.tags).__name__}"
    )
    assert isinstance(consolidated.consolidated_from, list), (
        "consolidated_from should be list, got "
        f"{type(consolidated.consolidated_from).__name__}"
    )
    assert isinstance(consolidated.metadata, dict), (
        f"metadata should be dict, got {type(consolidated.metadata).__name__}"
    )

    # Lineage points back at the three archived members (order-independent).
    assert set(consolidated.consolidated_from) == set(ids)
    # Tag union across the cluster is preserved.
    assert set(consolidated.tags) == {"tag-0", "tag-1", "tag-2", "shared"}

    # The originals are archived, not left in cold.
    assert {m.id for m in get_memories_by_tier(db_conn, "archived")} == set(ids)


def test_consolidated_columns_are_single_encoded(
    db_conn: sqlite3.Connection, mock_encoder
) -> None:
    """Guards the exact defect at the storage boundary: the stored column must
    be single-encoded so that *one* ``json.loads`` yields native types. A
    double-encoded column decodes to a ``str``/``str``/``str`` instead.
    """
    ids = ["c1", "c2", "c3"]
    _seed_cold_cluster(db_conn, ids, vec=[0.2] * 8)

    run_consolidation(db_conn, mock_encoder, min_cluster_size=3)

    row = db_conn.execute(
        "SELECT tags, consolidated_from, metadata FROM memories WHERE tier = 'warm'"
    ).fetchone()
    assert isinstance(json.loads(row["tags"]), list)
    assert isinstance(json.loads(row["consolidated_from"]), list)
    assert isinstance(json.loads(row["metadata"]), dict)
