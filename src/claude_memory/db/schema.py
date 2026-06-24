"""DDL definitions and migration logic for claude-memory."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

# Default embedding dimension matches all-MiniLM-L6-v2.
_DEFAULT_EMBEDDING_DIM = 384


def get_schema_sql(embedding_dim: int = _DEFAULT_EMBEDDING_DIM) -> str:
    """Return the full DDL for the claude-memory database.

    Parameters
    ----------
    embedding_dim:
        Dimensionality of the embedding vectors stored in *memories_vec*.
        Must match the sentence-transformer model in use.
    """
    return f"""\
-- ── Core table ──────────────────────────────────────────────────────────
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
    metadata          TEXT DEFAULT '{{}}'
);

-- ── Full-text search ────────────────────────────────────────────────────
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    summary,
    tags,
    content='memories',
    content_rowid='rowid'
);

-- FTS sync triggers: keep memories_fts in lockstep with memories.
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

-- ── Vector search (sqlite-vec) ──────────────────────────────────────────
CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
    id TEXT PRIMARY KEY,
    embedding float[{embedding_dim}]
);

-- ── Indexes ─────────────────────────────────────────────────────────────
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

-- ── Schema version tracking ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""


def initialize_db(
    conn: sqlite3.Connection,
    embedding_dim: int = _DEFAULT_EMBEDDING_DIM,
) -> None:
    """Create all tables, indexes, and triggers if they don't already exist.

    The caller is responsible for loading the sqlite-vec extension *before*
    calling this function (see :func:`connection.get_connection`).

    Parameters
    ----------
    conn:
        An open SQLite connection with the sqlite-vec extension loaded.
    embedding_dim:
        Dimensionality of embedding vectors — must match the model.
    """
    ddl: str = get_schema_sql(embedding_dim)
    conn.executescript(ddl)

    # Ensure the schema_version row exists.
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
