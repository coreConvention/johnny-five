"""Shared fixtures for claude-memory test suite.

Provides a mock embedding encoder and an in-memory SQLite database with
simplified tables so tests can run WITHOUT sentence-transformers or sqlite-vec.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import sys
import types
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import patch

import pytest

# Mock sqlite_vec before any claude_memory imports can trigger it.
_mock_sqlite_vec = types.ModuleType("sqlite_vec")
_mock_sqlite_vec.load = lambda conn: None  # type: ignore[attr-defined]
sys.modules["sqlite_vec"] = _mock_sqlite_vec

from claude_memory.db.queries import MemoryRecord, insert_memory


# ---------------------------------------------------------------------------
# Mock encoder — deterministic vectors based on text hash
# ---------------------------------------------------------------------------


class MockEncoder:
    """Drop-in replacement for EmbeddingEncoder that requires no ML libraries.

    Produces deterministic 384-dim vectors by hashing the input text.
    Similar texts will NOT get similar vectors with this approach (SHA-256 is
    a cryptographic hash), but that is acceptable for unit-testing logic paths.
    For tests that need *controllable* similarity, call ``encode_with_seed``
    or manually construct vectors.
    """

    def __init__(self, dim: int = 384) -> None:
        self._dim = dim

    def encode(self, text: str) -> list[float]:
        """Return a deterministic, normalised vector for *text*."""
        h: bytes = hashlib.sha256(text.encode()).digest()
        raw: list[float] = [float(b) / 255.0 for b in h]
        # Repeat until we have at least ``dim`` values, then truncate.
        raw = (raw * (self._dim // len(raw) + 1))[: self._dim]
        norm: float = math.sqrt(sum(x * x for x in raw))
        if norm == 0.0:
            return [0.0] * self._dim
        return [x / norm for x in raw]

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.encode(t) for t in texts]

    @property
    def dimension(self) -> int:
        return self._dim


# ---------------------------------------------------------------------------
# Brute-force cosine similarity search (replaces sqlite-vec MATCH)
# ---------------------------------------------------------------------------


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot: float = sum(x * y for x, y in zip(a, b))
    norm_a: float = math.sqrt(sum(x * x for x in a))
    norm_b: float = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def brute_force_vec_search(
    conn: sqlite3.Connection,
    embedding: list[float],
    top_k: int = 50,
) -> list[tuple[str, float]]:
    """Python-side brute-force vector search over the plain memories_vec table.

    Replaces the native sqlite-vec MATCH operator with a full scan + cosine
    similarity computation.  Returns ``(id, distance)`` tuples sorted by
    ascending distance (distance = 1 - cosine_similarity).
    """
    rows = conn.execute("SELECT id, embedding FROM memories_vec").fetchall()
    results: list[tuple[str, float]] = []
    for row in rows:
        stored_vec: list[float] = json.loads(row["embedding"])
        sim: float = _cosine_similarity(embedding, stored_vec)
        distance: float = 1.0 - sim
        results.append((row["id"], distance))
    results.sort(key=lambda r: r[1])
    return results[:top_k]


# ---------------------------------------------------------------------------
# Database fixture
# ---------------------------------------------------------------------------

# Schema DDL that replaces the sqlite-vec virtual table with a regular table.
_TEST_SCHEMA_SQL = """\
-- Core table (identical to production)
CREATE TABLE IF NOT EXISTS memories (
    id                TEXT PRIMARY KEY,
    content           TEXT NOT NULL,
    summary           TEXT,
    type              TEXT NOT NULL CHECK (type IN ('user', 'feedback', 'project', 'reference', 'lesson')),
    tags              TEXT DEFAULT '[]',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_accessed     TEXT NOT NULL,
    access_count      INTEGER DEFAULT 0,
    importance        REAL DEFAULT 5.0 CHECK (importance >= 0.0 AND importance <= 10.0),
    tier              TEXT DEFAULT 'hot' CHECK (tier IN ('hot', 'warm', 'cold', 'archived')),
    project_dir       TEXT,
    source_session    TEXT,
    supersedes        TEXT,
    consolidated_from TEXT DEFAULT '[]',
    metadata          TEXT DEFAULT '{}'
);

-- FTS5 table (real — SQLite ships with FTS5 built-in)
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    summary,
    tags,
    content='memories',
    content_rowid='rowid'
);

-- FTS sync triggers
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts (rowid, content, summary, tags)
    VALUES (NEW.rowid, NEW.content, NEW.summary, NEW.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts (memories_fts, rowid, content, summary, tags)
    VALUES ('delete', OLD.rowid, OLD.content, OLD.summary, OLD.tags);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts (memories_fts, rowid, content, summary, tags)
    VALUES ('delete', OLD.rowid, OLD.content, OLD.summary, OLD.tags);
    INSERT INTO memories_fts (rowid, content, summary, tags)
    VALUES (NEW.rowid, NEW.content, NEW.summary, NEW.tags);
END;

-- Regular table standing in for the vec0 virtual table
CREATE TABLE IF NOT EXISTS memories_vec (
    id TEXT PRIMARY KEY,
    embedding TEXT
);

-- Indexes (same as production)
CREATE INDEX IF NOT EXISTS idx_memories_type_importance
    ON memories (type, importance);
CREATE INDEX IF NOT EXISTS idx_memories_tier
    ON memories (tier);
CREATE INDEX IF NOT EXISTS idx_memories_project_dir
    ON memories (project_dir);
CREATE INDEX IF NOT EXISTS idx_memories_last_accessed
    ON memories (last_accessed);
CREATE INDEX IF NOT EXISTS idx_memories_created_at
    ON memories (created_at);
CREATE INDEX IF NOT EXISTS idx_memories_supersedes
    ON memories (supersedes);

-- Schema version
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
INSERT INTO schema_version (version) VALUES (1);
"""


@pytest.fixture()
def mock_encoder() -> MockEncoder:
    """Return a :class:`MockEncoder` instance (384-dim, no ML dependencies)."""
    return MockEncoder(dim=384)


@pytest.fixture()
def db_conn() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the test schema initialised.

    The ``search_vec`` function from ``claude_memory.db.queries`` is
    monkey-patched to use :func:`brute_force_vec_search` so that vector
    searches work without the sqlite-vec C extension.
    """
    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_TEST_SCHEMA_SQL)

    # Monkey-patch search_vec globally for the duration of this connection.
    with patch(
        "claude_memory.db.queries.search_vec",
        side_effect=lambda c, emb, top_k=50: brute_force_vec_search(c, emb, top_k),
    ):
        yield conn

    conn.close()


@pytest.fixture()
def sample_memories(
    db_conn: sqlite3.Connection,
    mock_encoder: MockEncoder,
) -> list[MemoryRecord]:
    """Insert 10 sample memories with varied types, tiers, and importance.

    Returns the list of :class:`MemoryRecord` objects that were inserted.
    """
    now: datetime = datetime.now(timezone.utc)
    records: list[MemoryRecord] = []

    samples: list[dict[str, Any]] = [
        {
            "id": "mem-001",
            "content": "User prefers dark mode in all editors.",
            "type": "user",
            "tags": ["preferences", "editor"],
            "importance": 8.0,
            "tier": "hot",
            "access_count": 15,
            "last_accessed": now.isoformat(),
        },
        {
            "id": "mem-002",
            "content": "Use Python 3.12 features like type parameter syntax.",
            "type": "project",
            "tags": ["python", "standards"],
            "importance": 7.5,
            "tier": "hot",
            "access_count": 10,
            "last_accessed": now.isoformat(),
        },
        {
            "id": "mem-003",
            "content": "Always validate inputs at the API boundary.",
            "type": "lesson",
            "tags": ["security", "validation"],
            "importance": 9.0,
            "tier": "hot",
            "access_count": 20,
            "last_accessed": now.isoformat(),
        },
        {
            "id": "mem-004",
            "content": "The project uses FastAPI for the REST API layer.",
            "type": "project",
            "tags": ["fastapi", "architecture"],
            "importance": 6.0,
            "tier": "warm",
            "access_count": 5,
            "last_accessed": (now - timedelta(days=15)).isoformat(),
        },
        {
            "id": "mem-005",
            "content": "Feedback: response times are too slow for large queries.",
            "type": "feedback",
            "tags": ["performance"],
            "importance": 4.0,
            "tier": "warm",
            "access_count": 3,
            "last_accessed": (now - timedelta(days=20)).isoformat(),
        },
        {
            "id": "mem-006",
            "content": "Reference documentation for the MCP protocol specification.",
            "type": "reference",
            "tags": ["mcp", "documentation"],
            "importance": 5.0,
            "tier": "warm",
            "access_count": 2,
            "last_accessed": (now - timedelta(days=25)).isoformat(),
        },
        {
            "id": "mem-007",
            "content": "Old lesson about using unittest instead of pytest.",
            "type": "lesson",
            "tags": ["testing", "deprecated"],
            "importance": 2.0,
            "tier": "cold",
            "access_count": 1,
            "last_accessed": (now - timedelta(days=100)).isoformat(),
        },
        {
            "id": "mem-008",
            "content": "Reference to an outdated API endpoint specification.",
            "type": "reference",
            "tags": ["api", "deprecated"],
            "importance": 1.5,
            "tier": "cold",
            "access_count": 0,
            "last_accessed": (now - timedelta(days=150)).isoformat(),
        },
        {
            "id": "mem-009",
            "content": "Archived note about the old database schema.",
            "type": "project",
            "tags": ["database", "schema"],
            "importance": 1.0,
            "tier": "archived",
            "access_count": 0,
            "last_accessed": (now - timedelta(days=200)).isoformat(),
        },
        {
            "id": "mem-010",
            "content": "User prefers concise commit messages.",
            "type": "user",
            "tags": ["preferences", "git"],
            "importance": 7.0,
            "tier": "hot",
            "access_count": 8,
            "last_accessed": now.isoformat(),
            "project_dir": "/home/user/my-project",
        },
    ]

    for s in samples:
        record = MemoryRecord(
            id=s["id"],
            content=s["content"],
            summary=None,
            type=s["type"],
            tags=json.dumps(s["tags"]),
            created_at=(now - timedelta(days=30)).isoformat(),
            updated_at=now.isoformat(),
            last_accessed=s["last_accessed"],
            access_count=s["access_count"],
            importance=s["importance"],
            tier=s["tier"],
            project_dir=s.get("project_dir"),
            source_session=None,
            supersedes=None,
            consolidated_from=json.dumps([]),
            metadata=json.dumps({}),
        )
        embedding: list[float] = mock_encoder.encode(s["content"])
        insert_memory(db_conn, record, embedding)
        records.append(record)

    return records
