#!/usr/bin/env python3
"""Export all memories from the johnny-five database to JSON on stdout.

Designed to run inside the johnny-five Docker container, where the SQLite
extension and the database path are already wired up:

    docker exec -i johnny-five python /app/setup/scripts/memory-export.py > backup.json

Or for a filtered export:

    docker exec -i johnny-five python /app/setup/scripts/memory-export.py \\
        --project-dir /path/to/project \\
        --tier hot --tier warm \\
        > project-active.json

Embeddings are NOT exported. They're rebuilt on import (deterministic given
the same model). This keeps the export portable across schema versions and
small (~500 bytes per memory).

Run on host (without Docker) only if you have a copy of memory.db locally and
have set MEMORY_DB_PATH to point at it; the script uses stdlib sqlite3 so
sentence-transformers is not required.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone


def _resolve_db_path() -> str:
    path = os.environ.get("MEMORY_DB_PATH", "/data/memory.db")
    if not os.path.exists(path):
        sys.exit(
            f"Database not found at {path}. "
            "Set MEMORY_DB_PATH or run inside the johnny-five container."
        )
    return path


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "content": row["content"],
        "summary": row["summary"],
        "type": row["type"],
        "tags": json.loads(row["tags"]) if row["tags"] else [],
        "importance": row["importance"],
        "tier": row["tier"],
        "project_dir": row["project_dir"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_accessed": row["last_accessed"],
        "access_count": row["access_count"],
        "source_session": row["source_session"],
        "supersedes": row["supersedes"],
        "consolidated_from": (
            json.loads(row["consolidated_from"]) if row["consolidated_from"] else []
        ),
        "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
    }


def export_memories(
    db_path: str,
    *,
    project_dir: str | None = None,
    tiers: list[str] | None = None,
    types: list[str] | None = None,
) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    where: list[str] = []
    params: list = []
    if project_dir is not None:
        where.append("project_dir = ?")
        params.append(project_dir)
    if tiers:
        placeholders = ",".join("?" * len(tiers))
        where.append(f"tier IN ({placeholders})")
        params.extend(tiers)
    if types:
        placeholders = ",".join("?" * len(types))
        where.append(f"type IN ({placeholders})")
        params.extend(types)

    sql = "SELECT * FROM memories"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return {
        "format_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_db": db_path,
        "filters": {
            "project_dir": project_dir,
            "tiers": tiers,
            "types": types,
        },
        "count": len(rows),
        "memories": [_row_to_dict(r) for r in rows],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export johnny-five memories to JSON on stdout.",
    )
    parser.add_argument(
        "--project-dir",
        help="Filter to memories scoped to this project directory.",
    )
    parser.add_argument(
        "--tier",
        action="append",
        choices=["hot", "warm", "cold", "archived"],
        help="Filter to one or more tiers (repeatable). Default: all tiers.",
    )
    parser.add_argument(
        "--type",
        action="append",
        choices=["user", "feedback", "project", "reference", "lesson"],
        help="Filter to one or more types (repeatable). Default: all types.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output (default: compact).",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path()
    payload = export_memories(
        db_path,
        project_dir=args.project_dir,
        tiers=args.tier,
        types=args.type,
    )

    if args.pretty:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")

    print(
        f"Exported {payload['count']} memories from {db_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
