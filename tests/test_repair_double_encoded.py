"""Tests for the double-JSON-encoded column repair (issue #12).

Root cause #11 / write-path fix #13: the old ``run_consolidation`` pre-encoded
``tags`` / ``consolidated_from`` / ``metadata`` with ``json.dumps`` before
handing them to ``insert_memory`` (which encodes once more), so those columns
were stored *double*-encoded and decoded (once) to a ``str`` instead of the
declared ``list`` / ``dict``. ``repair_double_encoded_json`` reverses the one
extra encoding on rows already written that way.

Seeding writes the RAW column bytes directly (bypassing ``insert_memory``'s
single-encode) so a row's on-disk encoding is exactly controlled. The
``db_conn`` fixture carries the real ``memories_au`` FTS trigger, so these tests
also exercise the trigger firing on the repair UPDATE.
"""

from __future__ import annotations

import json
import sqlite3

from claude_memory.db.migrations import repair_double_encoded_json
from claude_memory.db.queries import get_memory

_TS = "2026-01-01T00:00:00+00:00"


def _insert_raw(
    conn: sqlite3.Connection,
    *,
    id: str,
    tags: str | None,
    consolidated_from: str | None,
    metadata: str | None,
    updated_at: str = _TS,
    type: str = "lesson",
    tier: str = "warm",
) -> None:
    """Insert a row writing the RAW column strings verbatim.

    ``tags`` / ``consolidated_from`` / ``metadata`` are the exact text stored in
    the column — the caller decides whether that text is single- or
    double-encoded — so the repair sees precisely the on-disk encoding under test.
    """
    conn.execute(
        """
        INSERT INTO memories (
            id, content, summary, type, tags,
            created_at, updated_at, last_accessed,
            access_count, importance, tier,
            project_dir, source_session, supersedes,
            consolidated_from, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            id,
            f"content for {id}",
            None,
            type,
            tags,
            _TS,
            updated_at,
            _TS,
            0,
            5.0,
            tier,
            None,
            None,
            None,
            consolidated_from,
            metadata,
        ),
    )


def _double(value: object) -> str:
    """Return the double-JSON-encoded form of *value* (the #11 corruption)."""
    return json.dumps(json.dumps(value))


def _raw(conn: sqlite3.Connection, id: str, col: str) -> str | None:
    """Return the raw (undecoded) text stored in *col* for row *id*."""
    row = conn.execute(
        f"SELECT {col} FROM memories WHERE id = ?", (id,)  # noqa: S608 - test-local literal
    ).fetchone()
    return row[col]


# ── (a) a double-encoded row is repaired to native types ────────────────────


def test_double_encoded_row_repaired_to_native_types(
    db_conn: sqlite3.Connection,
) -> None:
    _insert_raw(
        db_conn,
        id="bad",
        tags=_double(["security", "validation"]),
        consolidated_from=_double(["mem-a", "mem-b"]),
        metadata=_double({"origin": "consolidation"}),
    )

    report = repair_double_encoded_json(db_conn)

    # Every affected column reported exactly one repair.
    assert report["tags"]["repaired"] == 1
    assert report["consolidated_from"]["repaired"] == 1
    assert report["metadata"]["repaired"] == 1

    # The public read path now yields native types with the correct values.
    got = get_memory(db_conn, "bad")
    assert got is not None
    assert got.tags == ["security", "validation"]
    assert isinstance(got.tags, list)
    assert got.consolidated_from == ["mem-a", "mem-b"]
    assert isinstance(got.consolidated_from, list)
    assert got.metadata == {"origin": "consolidation"}
    assert isinstance(got.metadata, dict)

    # The stored column is now single-encoded: one json.loads gives the container.
    assert isinstance(json.loads(_raw(db_conn, "bad", "tags")), list)
    assert isinstance(json.loads(_raw(db_conn, "bad", "consolidated_from")), list)
    assert isinstance(json.loads(_raw(db_conn, "bad", "metadata")), dict)


# ── (b) a healthy row is left untouched ─────────────────────────────────────


def test_healthy_row_untouched(db_conn: sqlite3.Connection) -> None:
    healthy_tags = json.dumps(["python", "standards"])
    healthy_from = json.dumps(["x", "y"])
    healthy_meta = json.dumps({"k": "v"})
    _insert_raw(
        db_conn,
        id="good",
        tags=healthy_tags,
        consolidated_from=healthy_from,
        metadata=healthy_meta,
    )

    report = repair_double_encoded_json(db_conn)

    # Nothing repaired…
    assert report["tags"]["repaired"] == 0
    assert report["consolidated_from"]["repaired"] == 0
    assert report["metadata"]["repaired"] == 0
    # …and the raw bytes are byte-for-byte unchanged.
    assert _raw(db_conn, "good", "tags") == healthy_tags
    assert _raw(db_conn, "good", "consolidated_from") == healthy_from
    assert _raw(db_conn, "good", "metadata") == healthy_meta


# ── (c) re-running is a no-op ───────────────────────────────────────────────


def test_rerun_is_noop(db_conn: sqlite3.Connection) -> None:
    _insert_raw(
        db_conn,
        id="bad",
        tags=_double(["a"]),
        consolidated_from=_double(["b"]),
        metadata=_double({}),
    )

    first = repair_double_encoded_json(db_conn)
    assert sum(c["repaired"] for c in first.values()) == 3

    # Second pass detects nothing to fix — idempotent by construction.
    second = repair_double_encoded_json(db_conn)
    assert second["tags"]["repaired"] == 0
    assert second["consolidated_from"]["repaired"] == 0
    assert second["metadata"]["repaired"] == 0

    # A dry-run over the repaired corpus also reports zero.
    dry = repair_double_encoded_json(db_conn, dry_run=True)
    assert sum(c["repaired"] for c in dry.values()) == 0


# ── dry-run detects without writing ─────────────────────────────────────────


def test_dry_run_counts_without_writing(db_conn: sqlite3.Connection) -> None:
    bad_tags = _double(["a", "b"])
    _insert_raw(
        db_conn,
        id="bad",
        tags=bad_tags,
        consolidated_from=_double(["c"]),
        metadata=_double({}),
    )

    report = repair_double_encoded_json(db_conn, dry_run=True)

    # Counted as needing repair…
    assert report["tags"]["repaired"] == 1
    assert report["consolidated_from"]["repaired"] == 1
    assert report["metadata"]["repaired"] == 1
    # …but nothing was written: the column is still double-encoded.
    assert _raw(db_conn, "bad", "tags") == bad_tags
    assert isinstance(json.loads(_raw(db_conn, "bad", "tags")), str)


# ── scanned counts + report shape ───────────────────────────────────────────


def test_report_shape_and_scanned_counts(db_conn: sqlite3.Connection) -> None:
    _insert_raw(
        db_conn,
        id="r1",
        tags=_double(["a"]),
        consolidated_from=json.dumps(["ok"]),
        metadata=json.dumps({}),
    )
    _insert_raw(
        db_conn,
        id="r2",
        tags=json.dumps(["ok"]),
        consolidated_from=json.dumps(["ok"]),
        metadata=json.dumps({}),
    )

    report = repair_double_encoded_json(db_conn, dry_run=True)

    # Every column is scanned once per row (== table row count).
    assert report["tags"]["scanned"] == 2
    assert report["consolidated_from"]["scanned"] == 2
    assert report["metadata"]["scanned"] == 2
    # Only the one genuinely double-encoded column is flagged.
    assert report["tags"]["repaired"] == 1
    assert report["consolidated_from"]["repaired"] == 0
    assert report["metadata"]["repaired"] == 0


# ── NULL / empty columns are skipped safely ─────────────────────────────────


def test_null_and_empty_columns_skipped(db_conn: sqlite3.Connection) -> None:
    # NULL and empty-string columns must not raise and must not be "repaired".
    _insert_raw(
        db_conn,
        id="nul",
        tags=None,
        consolidated_from="",
        metadata=None,
    )

    report = repair_double_encoded_json(db_conn)

    assert report["tags"]["repaired"] == 0
    assert report["consolidated_from"]["repaired"] == 0
    assert report["metadata"]["repaired"] == 0
    # Still scanned (examined), just found nothing to do.
    assert report["tags"]["scanned"] == 1


# ── partially-corrupt row: only the bad column is rewritten ─────────────────


def test_mixed_row_repairs_only_bad_column(db_conn: sqlite3.Connection) -> None:
    healthy_from = json.dumps(["keep", "me"])
    healthy_meta = json.dumps({"keep": True})
    _insert_raw(
        db_conn,
        id="mix",
        tags=_double(["fixme"]),
        consolidated_from=healthy_from,
        metadata=healthy_meta,
    )

    repair_double_encoded_json(db_conn)

    got = get_memory(db_conn, "mix")
    assert got is not None
    assert got.tags == ["fixme"]  # repaired
    # Healthy columns untouched, byte-for-byte.
    assert _raw(db_conn, "mix", "consolidated_from") == healthy_from
    assert _raw(db_conn, "mix", "metadata") == healthy_meta


# ── the repair does not bump updated_at (encoding fix, not a semantic edit) ──


def test_updated_at_preserved(db_conn: sqlite3.Connection) -> None:
    original_updated_at = "2020-05-05T05:05:05+00:00"
    _insert_raw(
        db_conn,
        id="bad",
        tags=_double(["a"]),
        consolidated_from=_double(["b"]),
        metadata=_double({}),
        updated_at=original_updated_at,
    )

    repair_double_encoded_json(db_conn)

    # A storage-encoding correction must leave the row's "last changed" stamp
    # (and, by the same principle, its retrieval signals) exactly where it was.
    assert _raw(db_conn, "bad", "updated_at") == original_updated_at
