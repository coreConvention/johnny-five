"""One-off, idempotent data-repair migrations for the claude-memory DB.

Hosts the repair for double-JSON-encoded ``list``/``dict`` columns written by
``run_consolidation`` *before* the write-path fix landed (root cause #11,
write-path fix #13, data repair #12).

The steady-state contract is that the storage layer owns JSON (de)serialization:
``insert_memory`` ``json.dumps()``-es ``tags`` / ``consolidated_from`` /
``metadata`` exactly once and ``_row_to_record`` ``json.loads()``-es them exactly
once (see :mod:`claude_memory.db.queries`). The old ``run_consolidation``
pre-serialized these fields, so they were encoded twice and decoded once —
leaving the column holding a JSON *string of* a JSON array/object. This module
detects and reverses that one extra encoding. It never hard-deletes and touches
no column other than the three above.

Run it against a live DB with::

    python -m claude_memory.db.migrations            # dry-run (no writes)
    python -m claude_memory.db.migrations --apply     # write, commit, re-verify

Back up the database before ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

# The columns whose JSON (de)serialization the DB layer owns. Consolidation rows
# written before #13 double-encoded exactly these; every other writer already
# passes native objects. This tuple is the single source of truth for both the
# scan and the UPDATE below — all entries are module constants, never user input.
_JSON_COLUMNS: tuple[str, ...] = ("tags", "consolidated_from", "metadata")


def repair_double_encoded_json(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
) -> dict[str, dict[str, int]]:
    """Repair rows whose ``tags`` / ``consolidated_from`` / ``metadata`` columns
    were double-JSON-encoded (issue #12).

    Detection is unambiguous for this schema: these columns always hold a JSON
    *list* / *dict*, so a value that decodes (once) to a ``str`` is
    double-encoded and the once-decoded inner string is exactly the correct
    single-encoded value. The repair writes that inner string back verbatim.

    Idempotent by construction: after repair the column decodes to a ``list`` /
    ``dict``, so a second pass sees ``isinstance(decoded, str) is False`` and
    does nothing.

    Only these three columns are written, and only the ones that actually need
    it. ``updated_at`` — and every other field — is left untouched: this is a
    storage-encoding correction, not a semantic edit, so the row's "last changed"
    timestamp and its retrieval signals (``last_accessed``) must not move. The
    ``memories_au`` AFTER UPDATE trigger re-syncs the FTS shadow copy
    automatically when ``tags`` is written — no manual FTS handling required.

    Mutates via *conn* but does **not** commit; the caller owns the transaction
    boundary (mirroring the rest of :mod:`claude_memory.db.queries`). With
    ``dry_run=True`` it counts what it *would* repair and issues no ``UPDATE``.

    Parameters
    ----------
    conn:
        An open connection to the memory database.
    dry_run:
        When ``True``, detect and count only — never write.

    Returns
    -------
    dict
        ``{column: {"scanned": int, "repaired": int}}``. ``scanned`` is the
        number of rows examined for that column (equal across columns — it is
        the table row count); ``repaired`` is how many were double-encoded.
    """
    report: dict[str, dict[str, int]] = {
        col: {"scanned": 0, "repaired": 0} for col in _JSON_COLUMNS
    }

    # Constant column list (no user input) — read the RAW column text, not a
    # decoded MemoryRecord, so we can inspect the on-disk encoding directly.
    select_cols: str = ", ".join(("id", *_JSON_COLUMNS))
    rows = conn.execute(f"SELECT {select_cols} FROM memories").fetchall()  # noqa: S608 - constant column list

    for row in rows:
        repairs: dict[str, str] = {}
        for col in _JSON_COLUMNS:
            report[col]["scanned"] += 1
            raw = row[col]
            if not raw:
                # NULL / empty column — nothing to decode, nothing to repair.
                continue
            try:
                decoded = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                # Not valid JSON at all: some other corruption, out of scope for
                # this repair. Leave it untouched for a human rather than guess.
                continue
            if isinstance(decoded, str):
                # Double-encoded: the once-decoded inner string is the correct
                # single-encoded value. Re-store it verbatim.
                repairs[col] = decoded
                report[col]["repaired"] += 1

        if repairs and not dry_run:
            # One atomic UPDATE per row, setting only the columns that need it,
            # so the memories_au trigger fires once and updated_at is preserved.
            set_clause: str = ", ".join(f"{col} = ?" for col in repairs)
            conn.execute(
                f"UPDATE memories SET {set_clause} WHERE id = ?",  # noqa: S608 - keys are the _JSON_COLUMNS whitelist
                [*repairs.values(), row["id"]],
            )

    return report


# ── CLI runner ────────────────────────────────────────────────────────────


def _format_report(label: str, report: dict[str, dict[str, int]]) -> str:
    """Render a per-column report for the console."""
    lines: list[str] = [f"[repair] {label}:"]
    for col, counts in report.items():
        lines.append(
            f"    {col:<18} scanned={counts['scanned']:<6} "
            f"repaired={counts['repaired']}"
        )
    total: int = sum(c["repaired"] for c in report.values())
    lines.append(f"    {'TOTAL repaired':<18} {total}")
    return "\n".join(lines)


def _run_cli(argv: list[str] | None = None) -> int:
    """Console entrypoint: dry-run by default; ``--apply`` writes and verifies.

    Reuses :func:`repair_double_encoded_json` so the exact code covered by the
    unit tests is what touches the live database.
    """
    parser = argparse.ArgumentParser(
        prog="python -m claude_memory.db.migrations",
        description=(
            "Repair double-JSON-encoded tags/consolidated_from/metadata rows "
            "(issue #12). Dry-run unless --apply is given."
        ),
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the database path (default: MEMORY_DB_PATH / config).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write repairs and commit. Without this flag, runs a dry-run only.",
    )
    args = parser.parse_args(argv)

    # Imported lazily so importing this module for the repair function alone does
    # not pull in settings/connection machinery.
    from claude_memory.config import get_settings
    from claude_memory.db.connection import get_connection

    db_path: Path = args.db_path or get_settings().resolve_db_path()
    print(f"[repair] database: {db_path}")

    conn: sqlite3.Connection = get_connection(db_path)
    try:
        # Always dry-run first so the operator sees the blast radius before any
        # write, whether or not --apply was passed.
        dry: dict[str, dict[str, int]] = repair_double_encoded_json(
            conn, dry_run=True
        )
        print(_format_report("dry-run", dry))
        total: int = sum(c["repaired"] for c in dry.values())

        if total == 0:
            print("[repair] nothing to repair - corpus is clean. No write performed.")
            return 0

        if not args.apply:
            print(
                f"[repair] {total} column-value(s) would be repaired. "
                "Back up the DB, then re-run with --apply to write."
            )
            return 0

        # Apply for real in a single transaction, then commit.
        applied: dict[str, dict[str, int]] = repair_double_encoded_json(
            conn, dry_run=False
        )
        conn.commit()
        print(_format_report("applied", applied))

        # Prove idempotency on the same connection: a second pass must be a no-op.
        verify: dict[str, dict[str, int]] = repair_double_encoded_json(
            conn, dry_run=True
        )
        remaining: int = sum(c["repaired"] for c in verify.values())
        print(_format_report("verify (post-apply dry-run)", verify))
        if remaining != 0:
            print(
                f"[repair] ERROR: {remaining} still detected after apply - "
                "the repair is not idempotent on this data. Investigate."
            )
            return 1
        print("[repair] done - second pass is a no-op (idempotent).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(_run_cli())
