#!/usr/bin/env python3
"""Import memories from a JSON dump into the johnny-five database.

Reads JSON from stdin in the format produced by memory-export.py. Generates
fresh embeddings on insert (deterministic given the same model).

MUST run inside the johnny-five container (or in an environment with
sentence-transformers + sqlite-vec installed and the same model cached).

Usage:

    # Restore to a fresh DB (fail if any IDs already exist)
    docker exec -i johnny-five python /app/setup/scripts/memory-import.py \\
        < backup.json

    # Merge into existing DB (insert new, update existing by ID)
    docker exec -i johnny-five python /app/setup/scripts/memory-import.py --merge \\
        < shared-team-memories.json

    # Show what would happen, change nothing
    docker exec -i johnny-five python /app/setup/scripts/memory-import.py --dry-run \\
        < backup.json
"""
from __future__ import annotations

import argparse
import json
import sys

from claude_memory.config import get_settings
from claude_memory.db.connection import get_connection
from claude_memory.db.queries import MemoryRecord, get_memory, insert_memory, update_memory
from claude_memory.embeddings.encoder import get_encoder


def _record_from_dict(d: dict) -> MemoryRecord:
    return MemoryRecord(
        id=d["id"],
        content=d["content"],
        summary=d.get("summary"),
        type=d["type"],
        tags=d.get("tags", []) or [],
        created_at=d["created_at"],
        updated_at=d["updated_at"],
        last_accessed=d["last_accessed"],
        access_count=d.get("access_count", 0),
        importance=d.get("importance", 5.0),
        tier=d.get("tier", "hot"),
        project_dir=d.get("project_dir"),
        source_session=d.get("source_session"),
        supersedes=d.get("supersedes"),
        consolidated_from=d.get("consolidated_from", []) or [],
        metadata=d.get("metadata", {}) or {},
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import memories from a JSON dump on stdin.",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help=(
            "Update existing memories (by ID) instead of failing. "
            "Default: insert-only, fail on collision."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without modifying the database.",
    )
    args = parser.parse_args()

    payload = json.load(sys.stdin)
    if not isinstance(payload, dict) or "memories" not in payload:
        sys.exit("Input does not look like an export payload (missing 'memories' key).")

    memories = payload["memories"]
    if not memories:
        print("No memories to import.", file=sys.stderr)
        return 0

    settings = get_settings()
    conn = get_connection(settings.resolve_db_path(), settings.embedding_dim)
    encoder = get_encoder(settings.model_name)

    inserted = 0
    updated = 0
    skipped = 0
    errors: list[tuple[str, str]] = []

    try:
        for raw in memories:
            try:
                record = _record_from_dict(raw)
            except (KeyError, TypeError) as e:
                errors.append((raw.get("id", "?"), f"malformed record: {e}"))
                continue

            existing = get_memory(conn, record.id)

            if existing and not args.merge:
                errors.append((record.id, "ID collision (use --merge to update)"))
                continue

            if args.dry_run:
                action = "WOULD-UPDATE" if existing else "WOULD-INSERT"
                print(f"{action} {record.id} ({record.type}, importance={record.importance})", file=sys.stderr)
                if existing:
                    updated += 1
                else:
                    inserted += 1
                continue

            embedding = encoder.encode(record.content)
            if hasattr(embedding, "tolist"):
                embedding = embedding.tolist()

            if existing:
                update_memory(
                    conn,
                    id=record.id,
                    content=record.content,
                    summary=record.summary,
                    type=record.type,
                    tags=record.tags,
                    importance=record.importance,
                    tier=record.tier,
                    project_dir=record.project_dir,
                    metadata=record.metadata,
                )
                conn.execute(
                    "UPDATE memories_vec SET embedding = ? WHERE id = ?",
                    (json.dumps(embedding), record.id),
                )
                updated += 1
            else:
                insert_memory(conn, record, embedding)
                inserted += 1

        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    skipped = len(errors)
    print(
        f"\nImport summary: inserted={inserted}, updated={updated}, skipped={skipped}",
        file=sys.stderr,
    )
    if errors:
        print("\nErrors:", file=sys.stderr)
        for memory_id, reason in errors[:20]:
            print(f"  {memory_id}: {reason}", file=sys.stderr)
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
