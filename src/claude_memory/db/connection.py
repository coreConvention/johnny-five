"""SQLite connection management with WAL mode and sqlite-vec support."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import sqlite_vec

from claude_memory.db.schema import initialize_db


def get_connection(
    db_path: Path,
    embedding_dim: int = 384,
) -> sqlite3.Connection:
    """Open a configured SQLite connection.

    The returned connection has:
    - WAL journal mode for concurrent readers
    - ``synchronous=NORMAL`` for a good durability/speed trade-off
    - 64 MB page cache
    - Foreign-key enforcement enabled
    - The sqlite-vec extension loaded

    All tables are created automatically via :func:`schema.initialize_db`
    if they don't already exist.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file.  Parent directories
        must already exist.
    embedding_dim:
        Dimensionality of the embedding vectors (must match the model).
    """
    conn: sqlite3.Connection = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Enable SQLite extension loading. On many Python builds (Windows in
    # particular) this is disabled by default, so ``sqlite_vec.load`` fails
    # with ``OperationalError: not authorized`` without this call. We toggle
    # it back off after loading the extension for defence in depth — once
    # sqlite-vec is loaded into this connection, no further extension loads
    # are expected or allowed.
    try:
        conn.enable_load_extension(True)
    except (AttributeError, sqlite3.NotSupportedError):
        # Python compiled without extension support; the next call will
        # raise a clearer error.
        pass

    # Load the sqlite-vec extension before any DDL that references vec0.
    sqlite_vec.load(conn)

    try:
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.NotSupportedError):
        pass

    # Performance / safety pragmas.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64 MB (negative = KiB)
    conn.execute("PRAGMA foreign_keys=ON")

    # Ensure schema is up to date.
    initialize_db(conn, embedding_dim=embedding_dim)

    return conn


@contextmanager
def db_session(
    db_path: Path,
    embedding_dim: int = 384,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that yields a connection and handles commit/rollback.

    Usage::

        with db_session(settings.resolve_db_path()) as conn:
            insert_memory(conn, record, embedding)
            # auto-committed on clean exit
    """
    conn: sqlite3.Connection = get_connection(db_path, embedding_dim=embedding_dim)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
